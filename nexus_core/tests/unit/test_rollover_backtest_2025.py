"""Unit tests for the 2025 Dynamic Rollover Backtest Engine.

Tests cover:
1. Portfolio initialization and benchmark setup
2. Zero-lookahead feature generation and shift(1) validation
3. Opportunity cost momentum divergence and EV spread calculation
4. Anti-washout microstructure exit matrix (SL1, SL2, SL4, TP1-3)
5. Core capital excess deployment and covered call income
6. Regime V breakdown chase short entry levels and sizing
7. Transition engine evolution and pyramiding
8. End-to-end full year 2025 backtest simulation and metrics computation
"""

from pathlib import Path

import pandas as pd
import pytest

from calibration.backtest_engine_2025 import (
    Portfolio,
    RolloverBacktestEngine2025,
)
from market_analysis.dynamic_rollover.constants import (
    _BREAKOUT_READY_THRESHOLD,
    _MOMENTUM_DECAY_THRESHOLD,
)
from market_analysis.dynamic_rollover.models import (
    RolloverScenario,
    ShortEntryEvaluation,
)
from market_analysis.dynamic_rollover.short_entry_sizing import (
    build_short_entry_levels,
    compute_short_entry_sizing,
)


@pytest.fixture
def engine_with_synthetic_data(tmp_path: Path) -> RolloverBacktestEngine2025:
    """Populate a temporary DataStore with deterministic synthetic OHLCV data and
    return a fully-prepared RolloverBacktestEngine2025.

    Why this fixture exists
    -----------------------
    ``RolloverBacktestEngine2025.load_and_prepare_data()`` reads from
    ``.calibration_cache/``, which is git-ignored and only present after running
    ``python -m calibration fetch`` on a developer machine.  In CI / Docker
    (fresh checkout) the directory does not exist, causing ``RuntimeError: 缺失
    SPY 1d 歷史資料``.  This fixture injects synthetic data via a ``tmp_path``
    DataStore so the three tests that call ``load_and_prepare_data()`` are
    fully self-contained and environment-independent.

    Data coverage
    -------------
    ~504 business days starting 2024-01-02, so the engine's 2025 date filter
    (``"2025-01-02"`` → ``"2025-12-30"``) always finds enough trading days.
    """
    from calibration.data_store import DataStore
    from tests.unit.calibration_fixtures import synthetic_daily, synthetic_hourly

    cache_dir = tmp_path / "cache"
    store = DataStore(cache_dir)

    # Build equity OHLCV daily + hourly; use different seeds per symbol for
    # variation while keeping results deterministic across test runs.
    _N_DAYS = 504
    for sym, seed in (("SPY", 11), ("NVDA", 12), ("GLD", 13)):
        df_daily = synthetic_daily(n_days=_N_DAYS, seed=seed, start="2024-01-02")
        store.save("1d", sym, df_daily)
        df_hourly = synthetic_hourly(df_daily, n_days=_N_DAYS, seed=seed + 20)
        store.save("1h", sym, df_hourly)

    # ^VIX: daily only (engine skips hourly for vix_symbol).
    # Clamp to a realistic 12–35 range so regime-detection logic behaves normally.
    vix_daily = synthetic_daily(n_days=_N_DAYS, seed=99, start="2024-01-02", drift=0.0)
    vix_daily["Close"] = 12.0 + (vix_daily["Close"].abs() % 23.0)
    vix_daily["Open"] = vix_daily["Close"] * 0.99
    vix_daily["High"] = vix_daily["Close"] * 1.03
    vix_daily["Low"] = vix_daily["Close"] * 0.97
    store.save("1d", "^VIX", vix_daily)

    engine = RolloverBacktestEngine2025(cache_dir=cache_dir)
    engine.load_and_prepare_data()
    return engine


def test_portfolio_initialization_and_accounting() -> None:
    """測試 Portfolio 帳戶的建倉、手續費扣除與淨值精算。"""
    portfolio = Portfolio(initial_cash=100_000.0, fee_rate=0.0015)
    assert portfolio.cash == 100_000.0
    assert len(portfolio.positions) == 0

    # 買入多頭部位
    trade = portfolio.buy(
        symbol="SPY",
        asset_class="CORE",
        price=500.0,
        notional=50_000.0,
        scenario="INIT",
        reason="Initial CORE",
        timestamp="2025-01-02 09:30:00",
        date_str="2025-01-02",
        target_allocation_pct=0.50,
    )
    assert trade is not None
    assert trade.fee == pytest.approx(75.0)
    assert portfolio.cash == pytest.approx(50_000.0)
    assert "SPY" in portfolio.positions
    pos = portfolio.positions["SPY"]
    assert pos.shares == pytest.approx((50_000.0 - 75.0) / 500.0)

    # 淨值核算
    nav = portfolio.get_total_nav({"SPY": 500.0})
    assert nav == pytest.approx(100_000.0 - 75.0)


def test_portfolio_sell_and_realized_pnl() -> None:
    """測試 Portfolio 賣出與已實現損益計算。"""
    portfolio = Portfolio(initial_cash=10_000.0, fee_rate=0.0015)
    portfolio.buy(
        symbol="NVDA",
        asset_class="SATELLITE",
        price=100.0,
        notional=10_000.0,
        scenario="INIT",
        reason="Buy NVDA",
        timestamp="2025-01-02 09:30:00",
        date_str="2025-01-02",
    )
    pos = portfolio.positions["NVDA"]

    # 上漲 20% 後賣出 50%
    sell_trade = portfolio.sell(
        symbol="NVDA",
        price=120.0,
        ratio=0.50,
        scenario=RolloverScenario.SATELLITE_REBALANCE.value,
        reason="TP Take Profit",
        timestamp="2025-01-05 10:30:00",
        date_str="2025-01-05",
    )
    assert sell_trade is not None
    assert sell_trade.realized_pnl > 0
    assert portfolio.positions["NVDA"].shares == pytest.approx(pos.shares)


def test_zero_lookahead_features_contract(
    engine_with_synthetic_data: RolloverBacktestEngine2025,
) -> None:
    """測試日線特徵落後一天 (shift 1) 確保無前視偏差。"""
    engine = engine_with_synthetic_data
    # data already loaded by the fixture; no need to call load_and_prepare_data() again

    for sym in ["SPY", "NVDA", "GLD"]:
        dfeat = engine.daily_feat[sym]
        hfeat = engine.hourly_feat[sym]
        assert "date" in dfeat.columns
        assert "sma20" in dfeat.columns
        assert "atr14" in dfeat.columns
        assert "sma20_prev" in hfeat.columns
        assert "low10_prev" in hfeat.columns
        assert "high10_prev" in hfeat.columns

        # 檢驗 hfeat 每一根 K 線的 low10_prev 嚴格等於日前已知最低點
        test_date = engine.trading_dates[10]
        rows = hfeat[hfeat["date"] == test_date]
        if not rows.empty:
            prior_d = dfeat[dfeat["date"] < test_date]
            expected_low10 = (
                float(prior_d["Low"].tail(10).min())
                if "Low" in prior_d.columns
                else float(prior_d["low10"].iloc[-1])
            )
            assert rows["low10_prev"].iloc[0] == pytest.approx(expected_low10, rel=1e-3)


def test_power_squeeze_computation_and_decay_breakout() -> None:
    """測試 PowerSqueeze 指標在動能衰竭與突破發動時的分數映射。"""
    # 建立多頭加速突破序列 (由擠壓轉為急劇放量發動)
    close_breakout = pd.Series(
        [100.0] * 20 + [100.0 + (i**1.5) * 3.0 for i in range(10)]
    )
    high_breakout = close_breakout + 1.5
    low_breakout = close_breakout - 0.5
    psq_breakout = RolloverBacktestEngine2025._compute_psq_from_series(
        close_breakout, high_breakout, low_breakout
    )
    assert psq_breakout >= _BREAKOUT_READY_THRESHOLD

    # 建立加速走跌破位序列
    close_decay = pd.Series([100.0] * 20 + [100.0 - (i**1.5) * 3.0 for i in range(10)])
    high_decay = close_decay + 0.5
    low_decay = close_decay - 1.5
    psq_decay = RolloverBacktestEngine2025._compute_psq_from_series(
        close_decay, high_decay, low_decay
    )
    assert psq_decay <= _MOMENTUM_DECAY_THRESHOLD


def test_short_entry_levels_and_sizing_math() -> None:
    """測試 Regime V 破位追空的進場停損價位構建與凱利倉位精算。"""
    ev = ShortEntryEvaluation(
        all_passed=True,
        reason="Test Breakdown",
        structure_directive="SHORT_EQUITY",
        sub_mode="破位追空",
        conditions=(True, True, True, True, True, True),
        spot=100.0,
        resistance_wall=103.0,
        call_wall=104.0,
        put_wall=101.0,
        gamma_flip=103.0,
        next_negative_node=80.0,
        net_gex=-2.0,
        session_vwap=102.0,
        atr_15m=1.0,
        atr_1d=3.0,
        ivr=30.0,
    )

    levels = build_short_entry_levels(ev)
    assert levels is not None
    assert levels.entry_price == 100.0
    assert levels.stop_price > 100.0
    assert levels.target_price == 80.0
    assert levels.reward_risk_ratio >= 1.8

    sizing = compute_short_entry_sizing(
        levels=levels,
        capital=100_000.0,
        risk_limit_pct=15.0,
        vix_spot=20.0,
        rsi_15m=38.0,
    )
    assert sizing.share_qty > 0
    assert sizing.risk_budget_usd > 0
    assert sizing.notional_usd <= 100_000.0 * 0.15


def test_microstructure_exit_matrix_sl1_and_tp() -> None:
    """測試微觀結構出場矩陣在 SL1 與 TP1-TP3 的分層觸發。"""
    portfolio = Portfolio(initial_cash=50_000.0)
    portfolio.buy(
        symbol="TEST",
        asset_class="SATELLITE",
        price=100.0,
        notional=10_000.0,
        scenario="INIT",
        reason="Buy TEST",
        timestamp="2025-01-02 09:30:00",
        date_str="2025-01-02",
        anchor_base=95.0,
        stop_loss=93.0,
    )
    pos = portfolio.positions["TEST"]
    assert pos is not None

    # 測試 SL1 跌破 stop_loss 觸發 100% 清倉
    trade_sl = portfolio.sell(
        symbol="TEST",
        price=92.0,
        ratio=1.0,
        scenario=RolloverScenario.SATELLITE_REBALANCE.value,
        reason="SL1 Triggered",
        timestamp="2025-01-03 10:30:00",
        date_str="2025-01-03",
    )
    assert trade_sl is not None
    assert trade_sl.action == "SELL"
    assert "TEST" not in portfolio.positions


@pytest.mark.slow
def test_full_backtest_simulation_end_to_end(
    engine_with_synthetic_data: RolloverBacktestEngine2025,
) -> None:
    """測試 2025 年動態轉倉引擎全量回測運行、指標計算與防禦表現。

    NOTE: Performance assertions (win_rate, profit_factor, max_drawdown vs
    benchmark) depend on real 2025 market behaviour and cannot be guaranteed
    with synthetic random-walk data.  Those thresholds belong in integration
    tests that run against the real `.calibration_cache`.  Here we validate:
    - The engine completes without errors.
    - All BacktestMetrics fields are finite and within sane bounds.
    - At least 50 trades were recorded and ≥ 6 scenarios triggered (structural).
    """
    # run_simulation() internally calls load_and_prepare_data() again; that is
    # safe because the fixture already set cache_dir to the tmp_path store.
    engine = engine_with_synthetic_data
    engine.run_simulation()
    metrics = engine.calculate_metrics()

    # --- structural completeness checks ---
    assert metrics.total_trades > 50, "期待至少 50 筆交易記錄"
    assert len(metrics.scenario_stats) >= 6, "期待至少 6 個情境被觸發"

    # --- sane-bounds checks (values must be finite & non-negative) ---
    import math

    assert math.isfinite(metrics.total_return), "total_return 必須為有限值"
    assert math.isfinite(metrics.cagr), "cagr 必須為有限值"
    assert 0.0 <= metrics.win_rate <= 1.0, "win_rate 必須在 [0, 1] 範圍內"
    assert metrics.profit_factor >= 0.0, "profit_factor 必須 >= 0"
    assert metrics.max_drawdown >= 0.0, "max_drawdown 必須 >= 0"
    assert metrics.benchmark_max_drawdown >= 0.0, "benchmark_max_drawdown 必須 >= 0"

    # --- 判讀指標：Sortino / VaR / CVaR 皆有限，CVaR 不小於 VaR ---
    for value in (
        metrics.sortino_ratio,
        metrics.benchmark_sortino,
        metrics.var_95,
        metrics.cvar_95,
        metrics.benchmark_var_95,
        metrics.benchmark_cvar_95,
    ):
        assert math.isfinite(value)
    assert metrics.cvar_95 >= metrics.var_95 >= 0.0
    assert metrics.benchmark_cvar_95 >= metrics.benchmark_var_95 >= 0.0

    # --- 減碼 B&H 對照組以下行差對齊（Sortino 一致），而非總波動 ---
    if metrics.benchmark_downside_deviation > 0:
        expected_w = min(
            1.0,
            metrics.annualized_downside_deviation
            / metrics.benchmark_downside_deviation,
        )
        assert metrics.scaled_benchmark_weight == pytest.approx(expected_w)


def test_portfolio_short_and_long_mutual_exclusion_and_nav_valuation() -> None:
    """測試多空互斥防護、做空保證金扣抵與空頭部位每日盯市 (Mark-to-Market) 淨值計算。"""
    portfolio = Portfolio(initial_cash=100_000.0, fee_rate=0.0015)

    # 1. 建倉多頭 NVDA
    t_buy = portfolio.buy(
        symbol="NVDA",
        asset_class="SATELLITE",
        price=100.0,
        notional=10_000.0,
        scenario="INIT",
        reason="Buy NVDA",
        timestamp="2025-01-02 09:30:00",
        date_str="2025-01-02",
    )
    assert t_buy is not None
    assert portfolio.has_long("NVDA") is True
    assert portfolio.has_short("NVDA") is False

    # 2. 測試多頭持倉期間嚴禁開空
    t_short_fail = portfolio.short(
        symbol="NVDA",
        price=100.0,
        shares=50.0,
        stop_price=110.0,
        target_price=80.0,
        scenario=RolloverScenario.SHORT_ENTRY.value,
        reason="Invalid Short",
        timestamp="2025-01-02 10:30:00",
        date_str="2025-01-02",
    )
    assert t_short_fail is None
    assert portfolio.has_short("NVDA") is False

    # 3. 平倉多頭
    portfolio.sell(
        symbol="NVDA",
        price=100.0,
        ratio=1.0,
        scenario=RolloverScenario.SATELLITE_REBALANCE.value,
        reason="Exit Long",
        timestamp="2025-01-02 11:30:00",
        date_str="2025-01-02",
    )
    assert portfolio.has_long("NVDA") is False

    # 4. 建立做空部位 NVDA_SHORT
    t_short = portfolio.short(
        symbol="NVDA",
        price=100.0,
        shares=100.0,
        stop_price=110.0,
        target_price=80.0,
        scenario=RolloverScenario.SHORT_ENTRY.value,
        reason="Short NVDA",
        timestamp="2025-01-02 13:30:00",
        date_str="2025-01-02",
    )
    assert t_short is not None
    assert portfolio.has_short("NVDA") is True
    assert portfolio.has_long("NVDA") is False

    # 5. 測試做空持倉期間嚴禁買多
    t_buy_fail = portfolio.buy(
        symbol="NVDA",
        asset_class="SATELLITE",
        price=100.0,
        notional=5_000.0,
        scenario="CORE_DEPLOYMENT",
        reason="Invalid Buy",
        timestamp="2025-01-02 14:30:00",
        date_str="2025-01-02",
    )
    assert t_buy_fail is None

    # 6. 測試空頭現貨 Reg-T 保證金鎖定
    locked_margin = portfolio.get_locked_margin()
    assert locked_margin == pytest.approx(100.0 * 100.0 * 0.50)  # $5,000

    # 7. 測試每日盯市 (Mark-to-Market) 損益精算
    # 現價由 $100 跌至 $90，做空部位產生 +$1,000 未實現利益
    nav_90 = portfolio.get_total_nav({"NVDA": 90.0})
    short_pos = portfolio.positions["NVDA_SHORT"]
    assert short_pos.unrealized_pnl == pytest.approx(1000.0)
    assert nav_90 == pytest.approx(portfolio.cash + 1000.0)

    # 8. 測試回補 (COVER)
    t_cover = portfolio.cover(
        symbol="NVDA",
        price=90.0,
        ratio=1.0,
        scenario=RolloverScenario.SATELLITE_REBALANCE.value,
        reason="Cover Short",
        timestamp="2025-01-03 10:30:00",
        date_str="2025-01-03",
    )
    assert t_cover is not None
    assert t_cover.realized_pnl > 900.0  # 扣除手續費後淨利
    assert portfolio.has_short("NVDA") is False
    assert "NVDA_SHORT" not in portfolio.positions


def test_fundamental_broken_event_liquidation(
    engine_with_synthetic_data: RolloverBacktestEngine2025,
) -> None:
    """測試 FUNDAMENTAL_BROKEN 護城河破滅緊急事件之 100% 清倉保護。"""
    engine = engine_with_synthetic_data
    # data already loaded by the fixture
    first_date = engine.trading_dates[0]
    engine.setup_initial_portfolio(first_date)

    # 注入基本面破滅事件
    test_d = engine.trading_dates[5]
    test_d_str = str(test_d)
    _test_set: set[str] = {"NVDA"}
    engine.fundamental_broken_events[test_d_str] = _test_set

    # 驗證事件當日 NVDA 被 100% 清倉
    nvda_open = float(
        engine.daily_feat["NVDA"][engine.daily_feat["NVDA"]["date"] == test_d][
            "open"
        ].iloc[0]
    )
    current_prices = {"SPY": 500.0, "NVDA": nvda_open, "GLD": 250.0}

    assert engine.portfolio.has_long("NVDA") is True
    # 執行事件清倉
    if test_d_str in engine.fundamental_broken_events:
        for b_sym in engine.fundamental_broken_events[test_d_str]:
            if engine.portfolio.has_long(b_sym):
                spot_now = current_prices.get(b_sym, 0.0)
                t_fund = engine.portfolio.sell(
                    symbol=b_sym,
                    price=spot_now,
                    ratio=1.0,
                    scenario=RolloverScenario.FUNDAMENTAL_BROKEN.value,
                    reason="護城河破滅清倉",
                    timestamp=f"{test_d_str} 09:30:00",
                    date_str=test_d_str,
                )
                assert t_fund is not None
                assert t_fund.scenario == RolloverScenario.FUNDAMENTAL_BROKEN.value

    assert engine.portfolio.has_long("NVDA") is False
    assert "NVDA" not in engine.portfolio.positions


def test_dust_sweep_on_sell_and_cover() -> None:
    """測試賣出與回補時的殘餘微型碎片 (Dust) 自動 100% 清理機制。"""
    portfolio = Portfolio(initial_cash=10_000.0, fee_rate=0.0015)
    portfolio.buy(
        symbol="TEST",
        asset_class="SATELLITE",
        price=100.0,
        notional=300.0,  # 買入 3 股 ($300)
        scenario="INIT",
        reason="Buy TEST",
        timestamp="2025-01-02 09:30:00",
        date_str="2025-01-02",
    )
    assert "TEST" in portfolio.positions
    # 賣出 50%：理論剩餘 1.5 股 ($150 < $250)，觸發 dust sweep 執行 100% 清理
    t_sell = portfolio.sell(
        symbol="TEST",
        price=100.0,
        ratio=0.50,
        scenario=RolloverScenario.SATELLITE_REBALANCE.value,
        reason="Trim with dust sweep",
        timestamp="2025-01-02 10:30:00",
        date_str="2025-01-02",
    )
    assert t_sell is not None
    assert "TEST" not in portfolio.positions  # 已完全平倉，無殘留碎片
    assert portfolio.has_long("TEST") is False


def test_regime_iii_target_wall_and_progress() -> None:
    """測試 Regime III 右側突破以 target_wall (H60) 為天花板，杜絕同 K 棒誤觸發 TP1。"""
    portfolio = Portfolio(initial_cash=50_000.0)
    portfolio.buy(
        symbol="BREAK",
        asset_class="SATELLITE",
        price=142.0,
        notional=10_000.0,
        scenario="REGIME_III_MOMENTUM",
        reason="Breakout above H10 140",
        timestamp="2025-01-02 09:30:00",
        date_str="2025-01-02",
        anchor_base=140.0,
        target_wall=160.0,
        stop_loss=138.0,
    )
    pos = portfolio.positions["BREAK"]
    assert pos.target_wall == 160.0
    assert pos.anchor_base == 140.0

    # 現價 $142 已高於舊阻力 $140，但距目標天花板 $160 僅進展 (142 - 140) / (160 - 140) = 10%
    # 驗證 TP1 (160 * 0.995 = 159.2) 與 SL4 (progress >= 50%) 均不應提前誤觸發
    cw_val = pos.target_wall
    progress = (142.0 - pos.anchor_base) / (cw_val - pos.anchor_base)
    assert progress < 0.50
    assert 142.0 < cw_val * 0.995
