"""
tests/unit/test_data_accuracy_golden_m.py

數據正確性修正 M1–M12 (含 IVR 未知連帶影響) 的 golden 值測試。
每個測試寫出具體輸入與手算期望值，並在註解列出手算過程。
"""

import json
from datetime import date, datetime, timedelta
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import numpy as np
import pandas as pd
import pytest

import market_time


def _et_today() -> date:
    return datetime.now(market_time.ny_tz).date()


def _beta_frames(n: int, beta: float) -> tuple[pd.DataFrame, pd.DataFrame]:
    rng = np.random.default_rng(42)
    spy_r = rng.normal(0, 0.01, n - 1)
    idx = pd.bdate_range("2025-01-02", periods=n)
    spy_close = 500.0 * np.exp(np.concatenate([[0.0], np.cumsum(spy_r)]))
    stk_close = 100.0 * np.exp(np.concatenate([[0.0], np.cumsum(beta * spy_r)]))
    return (
        pd.DataFrame({"Close": stk_close}, index=idx),
        pd.DataFrame({"Close": spy_close}, index=idx),
    )


# ===========================================================================
# M1：/dash 對沖狀態以美元名目比較 (|Δ| × SPY vs 資本 × 10%)
# ===========================================================================
def _dash_ctx(delta: float, capital: float = 100000.0) -> MagicMock:
    ctx = MagicMock()
    ctx.total_theta = 0.0
    ctx.monthly_expense = 0.0
    ctx.cash_reserve = 0.0
    ctx.is_professional_mode = True
    ctx.total_vanna = 0.0
    ctx.total_weighted_delta = delta
    ctx.capital = capital
    return ctx


def _field(embed: Any, prefix: str) -> str:
    for f in embed.fields:
        if f.name.startswith(prefix):
            return str(f.value)
    raise AssertionError(prefix)


def test_m1_dash_hedge_status_compares_dollar_notional() -> None:
    from cogs.embed_builders.portfolio_embeds import create_strategic_dash_embed

    # Beta-Delta +40 SPY 股 × $670 = $26,800 > 資本 $100,000 × 10% = $10,000
    # → 需調整。舊版直接比 40 < 10,000 (股數 vs 美元) → 永遠「運行中」。
    embed = create_strategic_dash_embed(
        _dash_ctx(40.0), {}, vix_spot=20.0, spy_price=670.0
    )
    assert "需調整" in _field(embed, "🛡️ 組合風險精算")
    # +10 股 × 670 = $6,700 < $10,000 → 運行中
    embed2 = create_strategic_dash_embed(
        _dash_ctx(10.0), {}, vix_spot=20.0, spy_price=670.0
    )
    assert "運行中" in _field(embed2, "🛡️ 組合風險精算")
    # SPY / VIX 未知 → 標示資料不足
    embed3 = create_strategic_dash_embed(_dash_ctx(40.0), {}, vix_spot=None)
    nro = _field(embed3, "🛡️ 組合風險精算")
    assert "SPY 現價未知" in nro and "VIX 資料不足" in nro


# ===========================================================================
# M2：NAV 按市價 (現金 + 現貨市值 + 期權市值，賣方權利金不重複計入)
# ===========================================================================
@pytest.mark.asyncio
async def test_m2_market_nav_golden() -> None:
    from services.trading_service import TradingService

    def _h(sym: str, qty: float, cost: float) -> MagicMock:
        h = MagicMock()
        h.symbol = sym
        h.metadata = {"quantity": qty, "avg_cost": cost}
        return h

    prices = {"AAPL": 210.0, "TSLA": 300.0, "ZZZ": 0.0}

    async def _quote(sym: str, *a: Any, **k: Any) -> dict[str, Any]:
        return {"c": prices[sym]}

    pnl_data = {"total_option_market_value": -300.0, "missing_quote_count": 0}
    with (
        patch(
            "services.asset_manager.AssetManager.get_assets",
            return_value=[
                _h("AAPL", 100, 150.0),
                _h("TSLA", -10, 250.0),
                _h("ZZZ", 5, 10.0),
            ],
        ),
        patch("services.market_data_service.get_quote", side_effect=_quote),
    ):
        nav = await TradingService(MagicMock()).get_market_nav(1, 20000.0, pnl_data)
    # 現金 20,000 + AAPL 100×210 (=21,000，按市價而非成本 15,000)
    # + TSLA -10×300 (=-3,000，空頭現貨為負債) + 期權 -300 (賣 1 口、mid 3.0)
    # = 37,700；ZZZ 報價缺失不計入並標示。
    assert nav["nav"] == pytest.approx(37700.0)
    assert nav["spot_market_value"] == pytest.approx(18000.0)
    assert nav["missing_spot_symbols"] == ["ZZZ"]
    assert nav["is_complete"] is False


# ===========================================================================
# M3：16:15 盤後 NAV/CVaR 重建時強制刷新日線
# ===========================================================================
@pytest.mark.asyncio
async def test_m3_post_market_rebuild_forces_history_refresh() -> None:
    from services import downside_risk_service as svc

    calls: list[dict[str, Any]] = []

    async def _hist(sym: str, period: str, **kw: Any) -> pd.DataFrame:
        calls.append({"sym": sym, **kw})
        return pd.DataFrame()

    with patch("services.market_data_service.get_history_df", side_effect=_hist):
        await svc._fetch_closes(["AAA"], force_refresh=True)
        await svc._fetch_closes(["BBB"])
    assert calls[0] == {"sym": "AAA", "force_refresh": True}
    assert calls[1] == {"sym": "BBB", "force_refresh": False}


# ===========================================================================
# M4：historical_iv 只寫 LIVE_IV；IVR 窗口為交易日；污染列清理
# ===========================================================================
def test_m4_migration_resets_historical_iv() -> None:
    import sqlite3

    from database.migrations import v088_reset_historical_iv as m

    conn = sqlite3.connect(":memory:")
    try:
        conn.execute("CREATE TABLE historical_iv (symbol TEXT, iv REAL, date TEXT)")
        rows = [
            ("AAA", 0.30, "2026-05-15"),
            ("AAA", 0.30, "2026-05-16"),  # 週末搬運列
            ("BBB", 0.25, "2026-05-18"),
        ]
        conn.executemany("INSERT INTO historical_iv VALUES (?, ?, ?)", rows)
        conn.executescript(m.sql)
        left = conn.execute("SELECT COUNT(*) FROM historical_iv").fetchone()[0]
    finally:
        conn.close()
    assert left == 0
    assert m.version == 88


def test_m4_recent_trading_dates_skip_weekends_and_holidays() -> None:
    # 2026-07-03 (五) 為獨立紀念日補假休市，07-04/05 週末；截至 07-07 (二)
    # 最近 3 個交易日 = 07-02、07-06、07-07。
    dates = market_time.get_recent_trading_dates(3, datetime(2026, 7, 7, 12, 0))
    assert dates == ["2026-07-02", "2026-07-06", "2026-07-07"]


@pytest.mark.asyncio
async def test_m4_stored_iv_not_written_back() -> None:
    from market_analysis.sentiment import iv_metrics
    from market_analysis.sentiment_engine import _iv_cache

    save_mock = AsyncMock()
    with (
        patch(
            "services.market_data_service.get_quote",
            new=AsyncMock(return_value={"c": 100.0}),
        ),
        patch(
            "market_analysis.sentiment.iv_metrics.is_market_open", return_value=False
        ),
        patch(
            "market_analysis.sentiment.iv_metrics.get_last_stored_iv", return_value=0.4
        ),
        patch("market_analysis.sentiment.iv_metrics.save_historical_iv", new=save_mock),
        patch(
            "market_analysis.sentiment.iv_metrics._calculate_straddle_implied_em",
            new=AsyncMock(return_value=None),
        ),
        patch(
            "market_analysis.sentiment.iv_metrics._calculate_iv_term_structure",
            new=AsyncMock(return_value=(None, None)),
        ),
        patch(
            "services.market_data_service.get_history_df",
            new=AsyncMock(return_value=pd.DataFrame()),
        ),
        patch("database.cache.get_kv_cache", return_value=None),
        patch("database.cache.save_kv_cache", new=AsyncMock()),
    ):
        _iv_cache.clear()
        res = await iv_metrics.fetch_and_calculate_iv_metrics(
            "ZZIV", force_refresh=True
        )
    # 盤後 STORED_IV (前值搬運 0.40) 只用於顯示，不得以今天日期寫回母體。
    assert res.iv_source == "STORED_IV"
    save_mock.assert_not_awaited()


# ===========================================================================
# M5：IV 反推補上 q；失敗時標記 greeks_stale
# ===========================================================================
@pytest.mark.asyncio
async def test_m5_refresh_greeks_inverts_iv_with_q(db_conn: Any) -> None:
    from py_vollib.black_scholes_merton import black_scholes_merton as bsm

    from market_analysis import portfolio

    expiry = (_et_today() + timedelta(days=30)).isoformat()
    meta = {
        "opt_type": "put",
        "strike": 95.0,
        "expiry": expiry,
        "entry_price": 2.0,
        "quantity": -1,
    }
    cur = db_conn.cursor()
    cur.execute(
        "INSERT INTO assets (user_id, symbol, context_type, metadata) "
        "VALUES (?, ?, 'TRADE', ?)",
        (8, "XYZ", json.dumps(meta)),
    )
    db_conn.commit()

    # 期權 mid 取 σ=0.35、q=0 的 BSM 理論價；chain IV 欄位為 0 → 走 IV 反推。
    mid = bsm("p", 100.0, 95.0, 30.0 / 365.0, 0.042, 0.35, 0.0)
    stk, spy = _beta_frames(120, 1.5)

    async def _hist(sym: str, *a: Any, **k: Any) -> pd.DataFrame:
        return spy if sym == "SPY" else stk

    async def _quote(sym: str, *a: Any, **k: Any) -> dict[str, Any]:
        return {"c": 500.0 if sym == "SPY" else 100.0}

    captured: list[Any] = []

    async def _write(batches: Any) -> None:
        captured.extend(batches)

    with (
        patch("config.RISK_FREE_RATE", 0.042),
        patch("market_analysis.greeks.RISK_FREE_RATE", 0.042),
        patch("services.market_data_service.get_history_df", side_effect=_hist),
        patch("services.market_data_service.get_quote", side_effect=_quote),
        patch(
            "services.market_data_service.get_dividend_yield_strict",
            new=AsyncMock(return_value=0.0),
        ),
        patch(
            "market_analysis.portfolio.get_option_chain_mid_iv",
            new=AsyncMock(return_value=(mid, 0.0, mid - 0.05, mid + 0.05)),
        ),
        patch("market_analysis.portfolio.execute_write_many_async", side_effect=_write),
    ):
        await portfolio.refresh_portfolio_greeks(8)

    _sql, rows, _commit = captured[0]
    out = json.loads(rows[0][0])
    # IV 反推成功 (舊版漏傳 q 永遠拋例外 → 保留舊 Greeks)：反推 σ≈0.35，
    # 每日 Theta = +$5.25 (同 H2 golden)。
    assert out["theta"] == pytest.approx(5.249, abs=0.01)
    assert out["greeks_stale"] is False
    # weighted_delta = Δ × (-1) × 100 × β(1.5) × S/SPY(100/500)
    assert out["weighted_delta"] == pytest.approx(
        out["delta"] * -1 * 100 * 1.5 * 0.2, abs=1e-3
    )
    assert out["beta_estimated"] is False


# ===========================================================================
# M6：Covered Call 權利金用 mid，不用 lastPrice
# ===========================================================================
def test_m6_cc_premium_uses_mid_not_last() -> None:
    from market_analysis.option_quote import resolve_option_mid

    # bid 1.55 / ask 1.65 → 1.60 (lastPrice 不參與)；零 bid ask 0.40 → 0.20 估
    assert resolve_option_mid(1.55, 1.65) == (pytest.approx(1.60), "MID")
    assert resolve_option_mid(0.0, 0.40) == (pytest.approx(0.20), "ASK_HALF")


# ===========================================================================
# M7：依目標 Delta 選約不可被 ±10% 履約價裁減扭曲
# ===========================================================================
def test_m7_high_iv_target_delta_needs_wider_pruning() -> None:
    from market_analysis.strategy.contract_selection import (
        _CONTRACT_SELECTION_PRUNE_PCT,
        _get_best_contract_data,
    )

    spot = 100.0
    strikes = [float(k) for k in range(60, 141)]
    puts = pd.DataFrame(
        {
            "strike": strikes,
            "impliedVolatility": [0.60] * len(strikes),
            "volume": [10] * len(strikes),
        }
    )

    def _pick(pct: float) -> float:
        lo, hi = spot * (1 - pct), spot * (1 + pct)
        c = MagicMock()
        c.puts = puts[(puts["strike"] >= lo) & (puts["strike"] <= hi)]
        c.calls = pd.DataFrame()
        with patch("market_analysis.greeks.RISK_FREE_RATE", 0.042):
            best, _ = _get_best_contract_data(c, "put", -0.20, spot, 40)
        return float(best["bs_delta"])

    # 40 DTE、σ=0.60：-0.20Δ 的履約價 K = 100·e^-(0.8416·0.1986 − 0.0243) ≈ 86.7
    # (現價 -13.3%)。±10% 裁減後最深只到 K=90 → Δ ≈ -0.257 (比標示深)；
    # ±35% 可選到 K=87 → Δ ≈ -0.205。
    assert _pick(0.10) == pytest.approx(-0.257, abs=0.005)
    assert _CONTRACT_SELECTION_PRUNE_PCT == 0.35
    assert _pick(_CONTRACT_SELECTION_PRUNE_PCT) == pytest.approx(-0.205, abs=0.005)


# ===========================================================================
# M8：總經備援常數不得讓 fail-closed 失效
# ===========================================================================
def test_m8_macro_top_escape_unknown_inputs_not_normal() -> None:
    from market_analysis.index_microstructure import evaluate_macro_top_escape_score

    _, tier, _, _ = evaluate_macro_top_escape_score(0.85, 40.0, 0.5, False)
    assert tier == "NORMAL"
    # Fear & Greed 未知 (舊版補 48 → NORMAL)：無法排除 WATCH → UNKNOWN
    score, tier2, _, factors = evaluate_macro_top_escape_score(0.85, None, 0.5, False)
    assert (score, tier2) == (0, "UNKNOWN")
    assert any("資料不足" in v for _, v in factors)
    # VTS 未知但已知分數已達 WATCH (F&G 80 極度貪婪) → 維持已知下限 WATCH
    _, tier3, _, _ = evaluate_macro_top_escape_score(None, 80.0, 0.5, False)
    assert tier3 == "WATCH"


def test_m8_three_valued_and() -> None:
    from market_analysis.index_microstructure import _and3

    assert _and3(True, True) is True
    assert _and3(True, None) is None
    assert _and3(False, None) is False


@pytest.mark.asyncio
async def test_m8_regime_three_valued_with_unknown_vix() -> None:
    from market_analysis import index_microstructure as im

    async def _run(spy: float, flip: float) -> str:
        with (
            patch(
                "services.market_data_service.get_vix_spot_strict",
                new=AsyncMock(return_value=None),
            ),
            patch(
                "services.market_data_service.get_vix_term_structure",
                new=AsyncMock(return_value={"is_valid": False}),
            ),
            patch(
                "services.market_data_service.get_quote",
                new=AsyncMock(return_value={"c": spy}),
            ),
            patch.object(
                im,
                "fetch_gex_metrics",
                new=AsyncMock(return_value={"gamma_flip": flip}),
            ),
            patch.object(
                im,
                "fetch_liquidity_metrics",
                new=AsyncMock(return_value={"ted_spread": 0.15, "_is_fallback": True}),
            ),
        ):
            return await im._compute_market_regime_uncached()

    # SPY 670 > Flip 660：兩種危機都需要「SPY < Flip」→ 確定不成立 → NORMAL
    # (即使 VIX/VTS/TED 未知)。舊版以 spy 510 / flip 515 備援時危機永遠不觸發。
    assert await _run(670.0, 660.0) == "NORMAL"
    # SPY 650 < Flip 660 且 VIX 未知：無法排除 SHORT_GAMMA_CRITICAL → UNKNOWN
    assert await _run(650.0, 660.0) == "UNKNOWN"


# ===========================================================================
# M9：Skew 分位未知時跳過指標，不補 50；None 不得造成 TypeError
# ===========================================================================
def test_m9_market_condition_unknowns() -> None:
    from models.execution import MarketCondition

    base: dict[str, Any] = dict(
        skew_percent=0.0, asset_price=100.0, ma20=100.0, atr_14=2.0, rsi_14=50.0
    )
    with pytest.raises(Exception):
        MarketCondition(vix=None, **base)  # type: ignore[arg-type]  # 舊版補 15.0
    mc = MarketCondition(vix=20.0, skew_percentile=None, **base)
    assert mc.skew_percentile is None  # 舊版補 50.0


def test_m9_euphoria_ratio_skips_unknown_skew() -> None:
    from market_analysis.dynamic_rollover.macro_top_escape_defense import (
        _compute_satellite_euphoria_ratio,
    )

    assets = [
        {
            "asset_class": "SATELLITE",
            "spot_price": 100.0,
            "call_wall": 0.0,
            "skew": None,
            "skew_percentile": None,
        },
        {
            "asset_class": "SATELLITE",
            "spot_price": 100.0,
            "call_wall": 0.0,
            "skew": -0.3,
            "skew_percentile": 10.0,
        },
    ]
    # 第 1 檔 Skew 未知 → 不計亢奮 (舊版 float(None) 會 TypeError)；第 2 檔亢奮
    # → 1/2 = 0.5
    assert _compute_satellite_euphoria_ratio(assets) == pytest.approx(0.5)


# ===========================================================================
# M10：SPY 未知不得以備援常數換算曝險
# ===========================================================================
def test_m10_rehedge_skips_exposure_when_spy_unknown() -> None:
    from market_analysis.hedging import evaluate_rehedge_necessity

    u_ctx = MagicMock(capital=100000.0, total_weighted_delta=100.0, risk_limit=15.0)
    # 100 股：舊版 SPY 補 670 → 曝險 67% > 15% 觸發 RE_HEDGE；SPY/VIX 未知時不判定。
    assert evaluate_rehedge_necessity(u_ctx, {"price": 0.0}) is None
    res = evaluate_rehedge_necessity(u_ctx, {"price": 0.0, "spy_price": 670.0})
    assert res is not None and res["action"] == "RE_HEDGE"


# ===========================================================================
# M11：戰術曝險納入空頭現貨與裸賣 Call
# ===========================================================================
def test_m11_tactical_exposure_counts_shorts_and_naked_calls() -> None:
    from market_analysis.signal_calculator import compute_deployed_tactical_value

    spot = [
        {"symbol": "AAPL", "quantity": -50.0, "avg_cost": 200.0},
        {"symbol": "NVDA", "quantity": 100.0, "avg_cost": 100.0},
        {"symbol": "AMD", "quantity": 100.0, "avg_cost": 100.0},
    ]
    options = [
        {"symbol": "TSLA", "opt_type": "call", "strike": 300.0, "quantity": -2.0},
        {"symbol": "NVDA", "opt_type": "call", "strike": 150.0, "quantity": -1.0},
        {"symbol": "AMD", "opt_type": "call", "strike": 120.0, "quantity": -3.0},
    ]
    # AAPL 空頭 |−50|×200 = 10,000 (舊版漏算)
    # NVDA 100×100 = 10,000；1 口 Covered Call 有 100 股擔保 → 0
    # AMD 100×100 = 10,000；3 口 Call 只有 1 口有擔保 → 2×120×100 = 24,000
    # TSLA 裸賣 2 口 → 2×300×100 = 60,000 (舊版漏算)
    # 合計 114,000
    assert compute_deployed_tactical_value(
        spot_holdings=spot, option_positions=options
    ) == pytest.approx(114000.0)


# ===========================================================================
# M12：PCR 缺值不補 0.8；油價未知取保守；期權資料時效標示
# ===========================================================================
def test_m12_pcr_unknown_skips_adjustment() -> None:
    from cogs.embed_builders._core import OPTION_DATA_TIMING_NOTE
    from market_analysis.risk_engine import MacroContext, get_macro_modifiers

    macro = MacroContext(vix=20.0, oil_price=70.0, vix_change=0.0, vts_ratio=0.9)
    # PCR 1.5 (>1.2) → w_regime ×0.8 = 0.8；PCR 未知 → 不修正 = 1.0
    assert get_macro_modifiers(macro, pcr=1.5)[2] == pytest.approx(0.8)
    assert get_macro_modifiers(macro, pcr=None)[2] == pytest.approx(1.0)
    # 油價未知 → 取最保守 0.5 (不以 75/85 冒充)
    assert get_macro_modifiers(MacroContext(vix=20.0, oil_price=None, vix_change=0.0))[
        1
    ] == pytest.approx(0.5)
    assert "前一交易日" in OPTION_DATA_TIMING_NOTE


# ===========================================================================
# 連帶：未知 IVR 為 None，不得誤判 IV 崩塌或建議單腳買方
# ===========================================================================
@pytest.mark.asyncio
async def test_unknown_ivr_does_not_fake_iv_crush() -> None:
    from cogs.trading.portfolio_monitor import PortfolioMonitorCog

    cog = PortfolioMonitorCog.__new__(PortfolioMonitorCog)
    cog.bot = MagicMock()
    save = AsyncMock()
    with (
        patch("database.cache.get_kv_cache", return_value=60.0),
        patch("database.cache.save_kv_cache", new=save),
    ):
        m = await cog._build_symbol_metrics(
            "AAPL", {"quote": {"c": 200.0}, "iv_metrics": {"iv_rank": None}}
        )
    # 前值 IVR 60、本輪未知：舊版把未知存成 0.0 → ivr_drop 60 (IV 崩塌快速出場)。
    assert m["ivr"] is None
    assert m["ivr_drop"] == 0.0
    save.assert_not_awaited()  # 不以未知覆寫前值


def test_unknown_ivr_entry_directive_prefers_spread() -> None:
    from market_analysis.dynamic_rollover.opportunity_cost import (
        _derive_entry_structure_directive,
    )

    # IVR 20 (≤ 門檻) → Long Call；IVR 未知 (舊版當 0 → Long Call) → 價差
    assert "Long Call" in _derive_entry_structure_directive(0.12, 20.0, None)
    assert "Bull Call Spread" in _derive_entry_structure_directive(0.12, None, None)
