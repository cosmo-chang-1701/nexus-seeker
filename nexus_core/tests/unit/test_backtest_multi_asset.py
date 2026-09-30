"""多資產回測一般化與 BOXX 大盤退場（合成資料、決定性）。"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd
import pytest

from calibration.backtest_analysis import (
    retreat_summary,
    symbol_contribution,
    yearly_metrics,
)
from calibration.backtest_engine_2025 import (
    BOXX_SYMBOL,
    RETREAT_SCENARIO,
    BacktestUniverse,
    RolloverBacktestEngine2025,
    equity_satellite,
    gold_satellite,
    legacy_universe,
    multi_asset_universe,
)
from calibration.data_store import DataStore
from tests.unit.calibration_fixtures import synthetic_daily, synthetic_hourly

pytestmark = pytest.mark.slow

_N = 360
_EXIT_START = 260  # 核心第一個收盤低於 200 日均線的交易日（序號）
_RECOVER = 276  # 核心第一個重新站回均線的交易日（序號）
_BOXX_LISTING = 280


def _dates() -> pd.DatetimeIndex:
    return pd.bdate_range("2021-01-04", periods=_N, tz="America/New_York")


def _core_daily() -> pd.DataFrame:
    close = np.full(_N, 100.0)
    close[_EXIT_START:_RECOVER] = 90.0
    close[_RECOVER:] = 110.0
    return pd.DataFrame(
        {
            "Open": close,
            "High": close * 1.001,
            "Low": close * 0.999,
            "Close": close,
            "Volume": np.full(_N, 2_000_000.0),
        },
        index=_dates(),
    )


def _flat(level: float, drift: float = 0.0) -> pd.DataFrame:
    close = level * np.exp(drift * np.arange(_N))
    return pd.DataFrame(
        {
            "Open": close,
            "High": close,
            "Low": close,
            "Close": close,
            "Volume": np.full(_N, 1_000_000.0),
        },
        index=_dates(),
    )


def _universe() -> BacktestUniverse:
    sats = (
        equity_satellite("AAA", 0.20),
        equity_satellite("BBB", 0.20),
        gold_satellite("GLD", 0.10),
    )
    return BacktestUniverse(
        name="TEST",
        core_symbol="VOO",
        core_weight=0.40,
        satellites=sats,
        cash_weight=0.10,
        benchmark_weights=(("VOO", 0.40),) + tuple((s.symbol, s.weight) for s in sats),
        benchmark_cash_weight=0.10,
        market_symbol="SPY",
        hourly_alignment="timestamp",
        rotation_policy="portfolio",
    )


def _store(tmp_path: Path, drop_bar: bool = False) -> tuple[str, str, list[Any]]:
    store = DataStore(tmp_path)
    core = _core_daily()
    store.save("1d", "VOO", core)
    store.save("1h", "VOO", synthetic_hourly(core, n_days=110, seed=1))
    store.save("1d", "SPY", synthetic_daily(n_days=_N, seed=2, start="2021-01-04"))
    for i, sym in enumerate(("AAA", "BBB", "GLD")):
        d = synthetic_daily(n_days=_N, seed=10 + i, start="2021-01-04", drift=0.0008)
        store.save("1d", sym, d)
        h = synthetic_hourly(d, n_days=110, seed=20 + i)
        if drop_bar and sym == "BBB":
            h = h.drop(h.index[-30])
        store.save("1h", sym, h)
    store.save("1d", "^VIX", _flat(15.0))
    store.save("1d", "BIL", _flat(90.0, drift=0.0002))
    store.save("1d", "BOXX", _flat(100.0, drift=0.0002).iloc[_BOXX_LISTING:])
    dates = list(_dates().date)
    return str(dates[250]), str(dates[-5]), dates


def _engine(tmp_path: Path, **kw: Any) -> tuple[RolloverBacktestEngine2025, list[Any]]:
    start, end, dates = _store(tmp_path, drop_bar=kw.pop("drop_bar", False))
    eng = RolloverBacktestEngine2025(
        cache_dir=tmp_path,
        start_date=start,
        end_date=end,
        mode=kw.pop("mode", "defensive"),
        universe=_universe(),
        **kw,
    )
    return eng, dates


# ------------------------------------------------------------------ 配置


def test_multi_asset_universe_matches_spec() -> None:
    u = multi_asset_universe()
    assert u.core_symbol == "VOO" and u.core_weight == 0.40
    assert u.market_symbol == "SPY"
    eq = [s for s in u.satellites if not s.retreat_exempt]
    assert [s.symbol for s in eq] == [
        "NVDA",
        "META",
        "GOOGL",
        "TSLA",
        "MU",
        "PLTR",
        "FCX",
        "MRNA",
    ]
    assert all(s.weight == 0.06 and s.allow_short for s in eq)
    gld = u.satellites[-1]
    assert gld.symbol == "GLD" and gld.weight == 0.07 and not gld.allow_short
    total = u.core_weight + sum(s.weight for s in u.satellites) + u.cash_weight
    assert total == pytest.approx(1.0)
    bench = sum(w for _s, w in u.benchmark_weights) + u.benchmark_cash_weight
    assert bench == pytest.approx(1.0)


def test_legacy_universe_keeps_original_priority_and_short_scope() -> None:
    u = legacy_universe(0.5, 0.25, 0.15, 0.10)
    assert u.core_deploy_priority == ("GLD", "NVDA")
    assert [s.symbol for s in u.satellites if s.allow_short] == ["NVDA"]
    assert u.hourly_alignment == "position"


# ------------------------------------------------------------------ BOXX 退場


def test_retreat_signal_needs_three_prior_closes(tmp_path: Path) -> None:
    eng, dates = _engine(tmp_path, enable_boxx_retreat=True)
    eng.load_and_prepare_data()
    sig = eng.retreat_signal
    # 第 260、261 日收盤低於均線時，第 262 日開盤只看到 2 天 → 尚未觸發
    assert sig.get(dates[_EXIT_START + 2]) != "EXIT"
    assert sig[dates[_EXIT_START + 3]] == "EXIT"
    assert sig.get(dates[_RECOVER + 2]) != "ENTER"
    assert sig[dates[_RECOVER + 3]] == "ENTER"


def test_boxx_is_spliced_with_bil_before_listing(tmp_path: Path) -> None:
    eng, dates = _engine(tmp_path, enable_boxx_retreat=True)
    eng.load_and_prepare_data()
    f = eng.daily_feat[BOXX_SYMBOL].set_index("date")["close"]
    listing = dates[_BOXX_LISTING]
    assert eng.boxx_listing_date == listing
    assert f[listing] == pytest.approx(100.0 * np.exp(0.0002 * _BOXX_LISTING))
    # 上市前的每日報酬 = BIL 的每日報酬
    pre = f[[d for d in dates[_BOXX_LISTING - 5 : _BOXX_LISTING + 1]]]
    rets = pre.pct_change().dropna()
    # 快取以 float32 儲存價格，0.02% 的日報酬會有 ~1e-7 的量化誤差
    assert np.allclose(rets.to_numpy(), np.exp(0.0002) - 1.0, rtol=0.0, atol=1e-6)


def test_retreat_exit_and_reentry(tmp_path: Path) -> None:
    eng, dates = _engine(tmp_path, enable_boxx_retreat=True)
    eng.run_simulation()
    exit_day = str(dates[_EXIT_START + 3])
    enter_day = str(dates[_RECOVER + 3])

    assert len(eng.retreat_episodes) == 1
    ep = eng.retreat_episodes[0]
    assert ep["exit_date"] == exit_day and ep["enter_date"] == enter_day
    assert ep["days"] == (_RECOVER + 3) - (_EXIT_START + 3)

    exits = [
        t
        for t in eng.portfolio.trades
        if t.date == exit_day and t.scenario == RETREAT_SCENARIO
    ]
    sold = {t.symbol for t in exits if t.action == "SELL"}
    assert "VOO" in sold and "GLD" not in sold
    assert any(t.symbol == BOXX_SYMBOL and t.action == "BUY" for t in exits)
    core_sell = next(t for t in exits if t.symbol == "VOO")
    assert "50%" in core_sell.reason

    # 退場期間不得有任何衛星新進場、換股、加碼或核心部署到衛星
    blocked = {
        "REGIME_III_MOMENTUM",
        "REGIME_III_B_TREND_CONT",
        "REGIME_I_CATCH",
        "OPPORTUNITY_COST",
        "PYRAMID_ADD",
        "TRANSITION_ENGINE",
        "SHORT_ENTRY",
    }
    during = [
        t
        for t in eng.portfolio.trades
        if exit_day <= t.date < enter_day
        and t.action in ("BUY", "SHORT")
        and t.scenario in blocked
    ]
    assert not during
    assert not [
        t
        for t in eng.portfolio.trades
        if exit_day <= t.date < enter_day
        and t.scenario == "CORE_DEPLOYMENT"
        and t.action == "BUY"
    ]

    # 回場：賣出 BOXX、核心依目標權重重建
    enters = [
        t
        for t in eng.portfolio.trades
        if t.date == enter_day and t.scenario == RETREAT_SCENARIO
    ]
    assert any(t.symbol == BOXX_SYMBOL and t.action == "SELL" for t in enters)
    assert any(t.symbol == "VOO" and t.action == "BUY" for t in enters)
    assert (
        BOXX_SYMBOL not in eng.portfolio.positions
        or eng.portfolio.positions[BOXX_SYMBOL].shares == 0
    )


def test_benchmark_never_retreats(tmp_path: Path) -> None:
    eng, _dates = _engine(tmp_path, enable_boxx_retreat=True)
    eng.run_simulation()
    assert BOXX_SYMBOL not in eng.benchmark_shares
    ref, _ = _engine(tmp_path / "ref")
    ref.run_simulation()
    assert [r.benchmark_nav for r in eng.portfolio.daily_history] == [
        r.benchmark_nav for r in ref.portfolio.daily_history
    ]


def test_retreat_flag_off_has_no_boxx_activity(tmp_path: Path) -> None:
    eng, _dates = _engine(tmp_path)
    eng.run_simulation()
    assert not eng.retreat_episodes
    assert not [t for t in eng.portfolio.trades if t.scenario == RETREAT_SCENARIO]


# ------------------------------------------------------------------ 一般化


def test_timestamp_alignment_tolerates_missing_bar(tmp_path: Path) -> None:
    eng, _dates = _engine(tmp_path, drop_bar=True, mode="aggressive")
    eng.run_simulation()
    assert len(eng.portfolio.daily_history) == len(eng.trading_dates)


def test_market_proxy_is_spy_not_core(tmp_path: Path) -> None:
    eng, _dates = _engine(tmp_path)
    eng.load_and_prepare_data()
    assert eng.market_symbol == "SPY" and eng.core_symbol == "VOO"
    assert "SPY" in eng.daily_feat and "SPY" not in eng.hourly_feat


def test_core_deploy_candidates_by_psq_when_no_priority(tmp_path: Path) -> None:
    eng, _dates = _engine(tmp_path)
    assert eng._core_deploy_candidates({"AAA": 10.0, "BBB": 90.0, "GLD": 50.0}) == [
        "BBB",
        "GLD",
        "AAA",
    ]


# ------------------------------------------------------------------ 分析


def test_yearly_metrics_split_by_calendar_year(tmp_path: Path) -> None:
    eng, _dates = _engine(tmp_path)
    eng.run_simulation()
    years = yearly_metrics(eng.portfolio.daily_history)
    assert [s.label for s, _b in years] == sorted(
        {r.date[:4] for r in eng.portfolio.daily_history[1:]}
    )
    assert sum(s.days for s, _b in years) == len(eng.portfolio.daily_history) - 1


def test_symbol_contribution_merges_short_legs() -> None:
    class T:
        def __init__(self, sym: str, action: str, pnl: float, reason: str = "") -> None:
            self.symbol, self.action, self.realized_pnl = sym, action, pnl
            self.scenario, self.reason = "X", reason

    class P:
        unrealized_pnl = 5.0

    out = symbol_contribution(
        [T("MRNA", "SELL", -10.0, "🚨 SL1 結構失效"), T("MRNA_SHORT", "COVER", 3.0)],
        {"MRNA": P()},
    )
    assert out["MRNA"]["realized"] == -7.0
    assert out["MRNA"]["total"] == -2.0
    assert out["MRNA"]["stop_exits"] == 1


def test_retreat_summary_whipsaw() -> None:
    eps = [
        {
            "exit_date": "2022-01-10",
            "exit_idx": 5,
            "days": 10,
            "exit_core_px": 100.0,
            "exit_boxx_px": 100.0,
            "enter_date": "2022-01-24",
            "enter_core_px": 104.0,
            "enter_boxx_px": 100.2,
        },
        {
            "exit_date": "2022-03-01",
            "exit_idx": 40,
            "exit_core_px": 100.0,
            "exit_boxx_px": 100.0,
            "enter_date": None,
            "enter_core_px": None,
            "enter_boxx_px": None,
        },
    ]
    out = retreat_summary(eps, last_core_px=90.0, last_boxx_px=100.5, total_days=60)
    assert out["exits"] == 2 and out["entries"] == 1
    assert out["days_in_retreat"] == 10 + 20
    assert out["episodes"][0]["core_change"] == pytest.approx(0.04)
    assert out["episodes"][1]["core_change"] == pytest.approx(-0.10)
    assert len(out["missed_rebounds"]) == 1


# ------------------------------------------------------------------ 輪動頻率


def _rotation_engine(tmp_path: Path) -> tuple[RolloverBacktestEngine2025, Any]:
    eng, dates = _engine(tmp_path)
    eng.load_and_prepare_data()
    day = eng.trading_dates[5]
    for sym in ("AAA", "BBB", "GLD"):
        eng.portfolio.buy(
            symbol=sym,
            asset_class="SATELLITE",
            price=100.0,
            notional=10_000.0,
            scenario="INIT",
            reason="test",
            timestamp=f"{day} 09:30:00",
            date_str=str(day),
        )
    return eng, day


def test_portfolio_policy_executes_single_best_rotation_per_day(tmp_path: Path) -> None:
    """兩檔同時衰退、一檔突破：只執行 EV 價差最大的一筆，而非兩檔都轉入同一標的。"""
    eng, day = _rotation_engine(tmp_path)
    prices = {"AAA": 100.0, "BBB": 100.0, "GLD": 100.0}
    prev = {s: eng._get_daily_proxy_row(s, day) for s in prices}
    psq = {"AAA": 5.0, "BBB": 5.0, "GLD": 95.0}
    ev = {"AAA": 0.01, "BBB": 0.00, "GLD": 0.10}
    eng._rotate_portfolio_level(day, prices, prev, psq, ev, str(day))
    sells = [
        t
        for t in eng.portfolio.trades
        if t.scenario == "OPPORTUNITY_COST" and t.action == "SELL"
    ]
    assert [t.symbol for t in sells] == ["BBB"]  # 價差 0.10 > 0.09
    assert eng.last_portfolio_rotation_date == day


def test_portfolio_policy_cooldown_blocks_next_rotation(tmp_path: Path) -> None:
    eng, day = _rotation_engine(tmp_path)
    prices = {"AAA": 100.0, "BBB": 100.0, "GLD": 100.0}
    prev = {s: eng._get_daily_proxy_row(s, day) for s in prices}
    psq = {"AAA": 5.0, "BBB": 5.0, "GLD": 95.0}
    ev = {"AAA": 0.01, "BBB": 0.00, "GLD": 0.10}
    eng._rotate_portfolio_level(day, prices, prev, psq, ev, str(day))
    idx = eng.trading_dates.index(day)
    nxt = eng.trading_dates[idx + 1]
    eng._rotate_portfolio_level(nxt, prices, prev, psq, ev, str(nxt))
    sells = [
        t
        for t in eng.portfolio.trades
        if t.scenario == "OPPORTUNITY_COST" and t.action == "SELL"
    ]
    assert len(sells) == 1
    later = eng.trading_dates[idx + eng.opp_cost_cd_days + 3]
    eng._rotate_portfolio_level(later, prices, prev, psq, ev, str(later))
    sells = [
        t
        for t in eng.portfolio.trades
        if t.scenario == "OPPORTUNITY_COST" and t.action == "SELL"
    ]
    assert len(sells) == 2


def test_multi_asset_universe_uses_portfolio_rotation() -> None:
    assert multi_asset_universe().rotation_policy == "portfolio"
    assert legacy_universe(0.5, 0.25, 0.15, 0.10).rotation_policy == "per_source"
