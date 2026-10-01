"""
tests/unit/test_data_accuracy_golden.py

數據正確性修正 (H1–H5 / M1–M12 / L1–L13) 的 golden 值測試。

每個測試寫出具體輸入與手算期望值，並在註解列出手算過程；目的在於鎖住
「錯誤值 → 正確值」的修正，避免日後回歸。
"""

import json
import math
from datetime import date, datetime, timedelta, timezone
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import numpy as np
import pandas as pd
import pytest

import market_time


def _et_today() -> date:
    return datetime.now(market_time.ny_tz).date()


# ===========================================================================
# H1：期權持倉 DTE 依合約自身 expiry 計算 (ET 日期差)
# ===========================================================================
def test_h1_days_to_expiry_uses_et_calendar_date() -> None:
    # 到期日 2026-01-16；美東 2026-01-15 15:30 (naive 視為 ET) → 1 天。
    assert (
        market_time.days_to_expiry_et("2026-01-16", datetime(2026, 1, 15, 15, 30)) == 1
    )
    # 2026-01-16 03:00 UTC = 2026-01-15 22:00 ET (EST, UTC-5) → 仍是 1 天，
    # 不可用 UTC 日期 (會算成 0 天)。
    as_of_utc = datetime(2026, 1, 16, 3, 0, tzinfo=timezone.utc)
    assert market_time.days_to_expiry_et("2026-01-16", as_of_utc) == 1
    # 到期當天：DTE 0，T 以 MIN_T_DAYS=1 天下限 → 1/365。
    assert market_time.years_to_expiry(
        "2026-01-16", datetime(2026, 1, 16, 10, 0)
    ) == pytest.approx(1.0 / 365.0)
    # 45 天：T = 45/365 = 0.123288
    assert market_time.years_to_expiry(
        "2026-03-02", datetime(2026, 1, 16, 10, 0)
    ) == pytest.approx(45.0 / 365.0)


def test_h1_option_entry_dte_from_contract_not_symbol_nearest_dte() -> None:
    from cogs.trading.portfolio_monitor import PortfolioMonitorCog
    from market_analysis.dynamic_rollover.structural_signals import (
        evaluate_option_dte_tier,
    )

    # SPY 每日都有到期合約 → radar nearest_dte 恆為 0。舊版把 0 當成部位 DTE，
    # 一張 45 天後到期的多頭 Call 會落入 dte<=1 的 EXPIRATION_SETTLEMENT_ALERT
    # (無條件 LIQUIDATE)。正確值：expiry - 今天(ET) = 45 → NORMAL_EXECUTION。
    expiry = (_et_today() + timedelta(days=45)).isoformat()
    metrics = {
        "spot_price": 670.0,
        "ivr": 30.0,
        "max_pain": 665.0,
        "put_wall": 650.0,
        "call_wall": 690.0,
        "is_uoa_sweep": False,
        "dte": 0,  # 標的層級 nearest_dte (錯誤來源)
    }
    entry = PortfolioMonitorCog._build_option_asset_entry(
        "SPY", 1.0, 12.0, 11.9, 12.1, metrics, None, 680.0, expiry, "call", 0.18
    )
    assert entry["dte"] == 45
    assert evaluate_option_dte_tier(entry["dte"], "MANAGE_EXISTING") == (
        "NORMAL_EXECUTION"
    )
    # 舊值 0 的後果 (對照)：強制結算。
    assert evaluate_option_dte_tier(0, "MANAGE_EXISTING") == (
        "EXPIRATION_SETTLEMENT_ALERT"
    )


@pytest.mark.asyncio
@patch("database.cache.save_kv_cache", new_callable=AsyncMock)
@patch("database.cache.get_kv_cache", return_value=None)
async def test_h1_symbol_metrics_never_carry_nearest_dte(
    _kv: MagicMock, _save: AsyncMock
) -> None:
    from cogs.trading.portfolio_monitor import PortfolioMonitorCog

    cog = PortfolioMonitorCog.__new__(PortfolioMonitorCog)
    cog.bot = MagicMock()
    # 週選標的 nearest_dte=3：舊版現貨部位會因 0<3<=5 誤觸 TP3；現貨 DTE 應為 99。
    metrics = await cog._build_symbol_metrics(
        "AAPL", {"quote": {"c": 200.0}, "nearest_dte": 3}
    )
    assert metrics["dte"] == 99


# ===========================================================================
# H2：Theta 不可再除以 365 (vollib analytical theta 已是每日值)
# ===========================================================================
def test_h2_vollib_theta_is_daily_golden() -> None:
    from market_analysis.greeks import calculate_greeks

    # S=100, K=95, T=30/365, σ=0.35, r=0.042, q=0，Put。
    # BSM 年化 theta ≈ -19.159/年；vollib 回傳 /365 後 = -0.052490/日。
    # 賣 1 口 (qty=-1)：-0.052490 × -1 × 100 = +$5.249 ≈ +$5.25/日。
    # 交叉驗證：BSM 價格 P(T=30/365)-P(T=29/365) ≈ 0.05286 (有限差分)。
    with patch("market_analysis.greeks.RISK_FREE_RATE", 0.042):
        g = calculate_greeks("put", 100.0, 95.0, 30.0 / 365.0, 0.35, 0.0)
    position_daily_theta = g["theta"] * -1 * 100
    assert position_daily_theta == pytest.approx(5.249, abs=0.005)
    # 舊版再除 365 的錯誤值：$0.0144/日 (縮小 365 倍)
    assert position_daily_theta / 365.0 == pytest.approx(0.01438, abs=0.0001)


@pytest.mark.asyncio
async def test_h2_after_market_report_total_theta_not_divided() -> None:
    from market_analysis.portfolio import PortfolioStatusOrchestrator

    orch = PortfolioStatusOrchestrator(user_capital=100000.0)
    orch.spy_price = 670.0
    expiry = (_et_today() + timedelta(days=30)).isoformat()
    chain = MagicMock()
    chain.calls = pd.DataFrame()
    chain.puts = pd.DataFrame(
        [{"strike": 95.0, "bid": 1.40, "ask": 1.50, "impliedVolatility": 0.35}]
    )
    row = ("XYZ", "put", 95.0, expiry, 2.0, -1, 0.0)
    with (
        patch("market_analysis.greeks.RISK_FREE_RATE", 0.042),
        patch(
            "market_analysis.portfolio.market_data_service.get_quote",
            new_callable=AsyncMock,
            return_value={"c": 100.0},
        ),
        patch(
            "market_analysis.portfolio.market_data_service.get_dividend_yield_strict",
            new_callable=AsyncMock,
            return_value=0.0,
        ),
        patch(
            "market_analysis.sentiment_engine.SentimentEngine.fetch_and_calculate_iv_metrics",
            new_callable=AsyncMock,
            side_effect=Exception("n/a"),
        ),
        patch(
            "market_analysis.portfolio.get_option_chain",
            new_callable=AsyncMock,
            return_value=chain,
        ),
    ):
        await orch._process_symbol_positions("XYZ", [row])
    # 同上 golden：+$5.25/日 (舊版為 +$0.0144/日)
    assert orch.total_theta == pytest.approx(5.249, abs=0.005)


def test_h2_user_context_sum_theta_not_divided(db_conn: Any) -> None:
    import database

    # DB 內 metadata.theta 已是「每部位每日 Theta」(+5.25 + -1.75 = +3.50)。
    # 舊版 /365 → +0.0096；正確值 +3.50。
    cur = db_conn.cursor()
    cur.execute(
        "INSERT INTO user_settings (user_id, cash_reserve) VALUES (?, ?)", (7, 1000.0)
    )
    for theta in (5.25, -1.75):
        meta = {
            "opt_type": "put",
            "strike": 95.0,
            "expiry": "2099-01-16",
            "entry_price": 2.0,
            "quantity": -1,
            "theta": theta,
        }
        cur.execute(
            "INSERT INTO assets (user_id, symbol, context_type, metadata) "
            "VALUES (?, ?, 'TRADE', ?)",
            (7, "XYZ", json.dumps(meta)),
        )
    db_conn.commit()
    ctx = database.get_full_user_context(7)
    assert ctx.total_theta == pytest.approx(3.50)


# ===========================================================================
# H3：Beta 需 >= 60 筆重疊日線；不足時回傳 None (不可默默 1.0)
# ===========================================================================
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


def test_h3_beta_strict_golden() -> None:
    from market_analysis.risk_engine import calculate_beta, calculate_beta_strict

    # 構造個股對數報酬 = 1.5 × SPY 對數報酬 → cov/var = 1.5 (精確)。
    stk, spy = _beta_frames(120, 1.5)
    assert calculate_beta_strict(stk, spy) == pytest.approx(1.5)
    # "60d" 只有約 41 根 → 不足 60 筆：strict 回傳 None；舊介面才退回 1.0。
    stk41, spy41 = _beta_frames(41, 1.5)
    assert calculate_beta_strict(stk41, spy41) is None
    assert calculate_beta(stk41, spy41) == 1.0


@pytest.mark.asyncio
async def test_h3_report_flags_estimated_beta() -> None:
    from market_analysis.portfolio import PortfolioStatusOrchestrator

    orch = PortfolioStatusOrchestrator(user_capital=100000.0)
    stk41, spy41 = _beta_frames(41, 1.5)
    orch.spy_hist = spy41
    with (
        patch(
            "market_analysis.portfolio.market_data_service.get_quote",
            new_callable=AsyncMock,
            return_value={"c": 100.0},
        ),
        patch(
            "market_analysis.portfolio.market_data_service.get_dividend_yield_strict",
            new_callable=AsyncMock,
            return_value=None,
        ),
    ):
        info = await orch._get_stock_info("XYZ", stk41)
    assert info["beta"] == 1.0
    assert orch.beta_estimated_symbols == ["XYZ"]
    # L3：股息率未知 → 以 0 計算並標示 (不再寫死 ETF 1.5%)
    assert info["dividend_yield"] == 0.0
    assert orch.dividend_unknown_symbols == ["XYZ"]


# ===========================================================================
# H4：VIX 未知不得捏造 18.0；VIX 改用即時 quote；空 DataFrame 不快取
# ===========================================================================
@pytest.mark.asyncio
async def test_h4_vix_strict_uses_live_quote_and_oil_is_independent() -> None:
    from services.market_data_service import fundamentals

    async def _quote(sym: str, *a: Any, **k: Any) -> dict[str, Any]:
        assert sym == "^VIX"
        return {"c": 21.37, "pc": 20.0}

    with (
        patch("services.market_data_service.get_quote", side_effect=_quote),
        patch(
            "services.market_data_service.get_history_df",
            new_callable=AsyncMock,
            return_value=pd.DataFrame(),  # 原油抓取失敗
        ),
    ):
        assert await fundamentals.get_vix_spot_strict() == 21.37
        macro = await fundamentals.get_macro_environment()
    # vix_change = (21.37 - 20.0) / 20.0 = 0.0685；原油失敗只影響 oil。
    assert macro == {"vix": 21.37, "oil": None, "vix_change": 0.0685}


@pytest.mark.asyncio
async def test_h4_vix_unknown_returns_none_not_18() -> None:
    from services.market_data_service import fundamentals

    with patch(
        "services.market_data_service.get_quote",
        new_callable=AsyncMock,
        return_value={},
    ):
        assert await fundamentals.get_vix_spot_strict() is None
        assert (await fundamentals.get_macro_environment())["vix"] is None


@pytest.mark.asyncio
async def test_h4_empty_history_not_cached() -> None:
    from services.market_data_service import history
    from services.market_data_service.caches import _history_cache

    key = ("ZZEMPTY", "5d", "1d")
    _history_cache.pop(key, None)
    with patch(
        "services.market_data_service.history._safe_yf_history",
        new_callable=AsyncMock,
        return_value=None,
    ):
        df = await history._fetch_history_uncached("ZZEMPTY", "5d", "1d", key)
    assert df.empty
    assert key not in _history_cache


def test_h4_optimize_position_risk_fail_closed_when_vix_unknown() -> None:
    from market_analysis.risk_engine import optimize_position_risk

    # 賣方：VIX 未知 → 0 口。
    sto = optimize_position_risk(
        0.0, 0.3, 100000.0, 500.0, 0.16, "STO_PUT", vix_unknown=True
    )
    assert sto.suggested_contracts == 0
    # 做空 (BTO_PUT, 合約 Δ=-1.0 → 投影 -1.0)：risk_limit 15% × 0.5 (VIX 未知
    # 保守乘數) = 7.5%；max_safe_shares = 100000×7.5%/500 = 15；
    # safe_qty = (-15 - 0) // -1.0 = 15 口 (VIX 已知但無 macro 時為 30 口)。
    short = optimize_position_risk(
        0.0, -1.0, 100000.0, 500.0, 0.16, "BTO_PUT", vix_unknown=True
    )
    assert short.suggested_contracts == 15
    # 做多 (BTO_CALL)：不放大，維持 risk_limit 基準 → 100000×15%/500 = 30 口。
    long = optimize_position_risk(
        0.0, 1.0, 100000.0, 500.0, 0.16, "BTO_CALL", vix_unknown=True
    )
    assert long.suggested_contracts == 30


# ===========================================================================
# H5：報價缺失不可顯示 ±100%；零 bid 用 ask/2 並標示，不用 lastPrice
# ===========================================================================
def test_h5_resolve_option_mid_rules() -> None:
    from market_analysis.portfolio import resolve_option_mid

    assert resolve_option_mid(1.00, 1.20) == (pytest.approx(1.10), "MID")
    # 零 bid、ask 0.40 → 0.20 (ask/2 估)，不採可能數日前的 lastPrice
    assert resolve_option_mid(0.0, 0.40) == (pytest.approx(0.20), "ASK_HALF")
    assert resolve_option_mid(0.0, 0.0) == (0.0, "MISSING")


@pytest.mark.asyncio
async def test_h5_missing_quote_excluded_from_pnl() -> None:
    from services.trading_service import TradingService

    def _asset(aid: int, sym: str, qty: int, entry: float) -> MagicMock:
        a = MagicMock()
        a.id, a.symbol, a.entry_price = aid, sym, entry
        a.metadata = {
            "opt_type": "call",
            "strike": 100.0,
            "expiry": "2099-01-16",
            "entry_price": entry,
            "quantity": qty,
        }
        return a

    quotes = {
        "AAA": {"mid": 0.0, "iv": 0.0, "bid": 0.0, "ask": 0.0, "source": "MISSING"},
        "BBB": {"mid": 3.0, "iv": 0.3, "bid": 2.9, "ask": 3.1, "source": "MID"},
    }

    async def _q(sym: str, *a: Any) -> dict[str, Any]:
        return quotes[sym]

    with (
        patch(
            "services.asset_manager.AssetManager.get_assets",
            return_value=[_asset(1, "AAA", 2, 5.0), _asset(2, "BBB", -1, 4.0)],
        ),
        patch("market_analysis.portfolio.get_option_chain_quote", side_effect=_q),
    ):
        res = await TradingService(MagicMock()).get_portfolio_pnl(1)
    by = {t["symbol"]: t for t in res["trades"]}
    # AAA 報價缺失：舊版 (0-5)×100×2 = -$1000 (-100%)；現在為未知且不計入。
    assert by["AAA"]["current_price"] is None
    assert by["AAA"]["unrealized_pnl"] is None and by["AAA"]["pnl_pct"] is None
    # BBB 賣方：(4.0-3.0)×100×1 = +$100
    assert by["BBB"]["unrealized_pnl"] == pytest.approx(100.0)
    assert res["total_unrealized_pnl"] == pytest.approx(100.0)
    assert res["missing_quote_count"] == 1
    # 期權市值 (帶號)：3.0×100×(-1) = -$300 (賣方為負債)
    assert res["total_option_market_value"] == pytest.approx(-300.0)


def test_h5_position_report_shows_dashes_for_missing_quote() -> None:
    from market_analysis.report_formatter import format_position_report

    line = format_position_report(
        "AAA",
        "2099-01-16",
        100.0,
        "call",
        "",
        5.0,
        None,
        None,
        30,
        12.5,
        "x",
        quantity=2,
        iv=0.3,
        iv_rank=None,
    )
    assert "-- (報價缺失)" in line
    assert "-100.00%" not in line
    # L7：IVR 未知顯示 `--` 而非 0.0%
    assert "30.0%/--" in line
    assert math.isfinite(12.5)
