"""
tests/unit/test_pr31_review_followups.py

PR #31 (數據正確性) code review 後續修正的回歸測試。
每個測試對應一項 review finding，寫出具體輸入與期望值。
"""

import time
from typing import Any, Optional
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


def _mock_http_client(payload: dict[str, Any]) -> MagicMock:
    res = MagicMock()
    res.status_code = 200
    res.json = MagicMock(return_value=payload)
    client = AsyncMock()
    client.get = AsyncMock(return_value=res)
    client.__aenter__ = AsyncMock(return_value=client)
    client.__aexit__ = AsyncMock(return_value=False)
    return client


# ===========================================================================
# 1. edge 備援常數不得被 core 當成真實值
# ===========================================================================
@pytest.mark.asyncio
@pytest.mark.parametrize(
    "edge_data",
    [
        # 新版 edge：整批失敗帶 is_fallback
        {"rrp": None, "sahm_rule": None, "fear_greed": None, "is_fallback": True},
        # 舊版 edge：整組常數
        {
            "rrp": 420.5,
            "fed_balance": 7.25,
            "uer": 4.0,
            "sahm_rule": 0.35,
            "fear_greed": 48.0,
        },
    ],
)
async def test_core_macro_rejects_edge_fallback(edge_data: dict[str, Any]) -> None:
    import config
    from market_analysis import index_microstructure as im

    save = AsyncMock()
    client = _mock_http_client({"status": "success", "data": edge_data})
    with (
        patch.object(config, "TUNNEL_URL", "http://mock-tunnel"),
        patch("database.cache.save_kv_cache", save),
        patch("httpx.AsyncClient", return_value=client),
    ):
        result = await im._fetch_core_macro_metrics_uncached()

    assert result["_is_fallback"] is True
    assert result["sahm_rule"] is None and result["fear_greed"] is None
    saved_keys = {c.args[0] for c in save.call_args_list}
    assert "macro_sahm_rule" not in saved_keys
    assert "macro_fear_greed" not in saved_keys


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "edge_data",
    [
        {"ted_spread": None, "sofr_90": None, "dtb3": None, "is_fallback": True},
        {"ted_spread": 0.15, "sofr_90": 5.3, "dtb3": 5.15, "high_yield_spread": 3.1},
    ],
)
async def test_liquidity_rejects_edge_fallback(edge_data: dict[str, Any]) -> None:
    import config
    from market_analysis import index_microstructure as im

    save = AsyncMock()
    client = _mock_http_client({"status": "success", "data": edge_data})
    with (
        patch.object(config, "TUNNEL_URL", "http://mock-tunnel"),
        patch("database.cache.save_kv_cache", save),
        patch("httpx.AsyncClient", return_value=client),
    ):
        result = await im.fetch_liquidity_metrics()

    assert result["_is_fallback"] is True
    assert "macro_ted_spread" not in {c.args[0] for c in save.call_args_list}


@pytest.mark.asyncio
async def test_liquidity_accepts_real_edge_data() -> None:
    import config
    from market_analysis import index_microstructure as im

    edge_data = {"ted_spread": 0.12, "sofr_90": 4.4, "dtb3": 4.28, "is_fallback": False}
    client = _mock_http_client({"status": "success", "data": edge_data})
    with (
        patch.object(config, "TUNNEL_URL", "http://mock-tunnel"),
        patch("database.cache.save_kv_cache", AsyncMock()),
        patch("httpx.AsyncClient", return_value=client),
    ):
        result = await im.fetch_liquidity_metrics()

    assert not result.get("_is_fallback")
    assert result["ted_spread"] == 0.12


# ===========================================================================
# 2 & 6. 大盤 regime：無 edge 部署不得恆為 UNKNOWN；過期 GEX 快取視為未知
# ===========================================================================
async def _run_regime(
    *,
    tunnel_url: str,
    gex: dict[str, Any],
    vix: Optional[float],
    spy: float = 670.0,
) -> str:
    import config
    from market_analysis import index_microstructure as im

    with (
        patch.object(config, "TUNNEL_URL", tunnel_url),
        patch(
            "services.market_data_service.get_vix_spot_strict",
            new=AsyncMock(return_value=vix),
        ),
        patch(
            "services.market_data_service.get_vix_term_structure",
            new=AsyncMock(return_value={"is_valid": True, "vts_ratio": 0.9}),
        ),
        patch(
            "services.market_data_service.get_quote",
            new=AsyncMock(return_value={"c": spy}),
        ),
        patch.object(im, "fetch_gex_metrics", new=AsyncMock(return_value=gex)),
        patch.object(
            im,
            "fetch_liquidity_metrics",
            new=AsyncMock(return_value={"ted_spread": 0.15, "_is_fallback": True}),
        ),
    ):
        return await im._compute_market_regime_uncached()


@pytest.mark.asyncio
async def test_regime_without_edge_calm_market_is_normal() -> None:
    # 無 TUNNEL_URL、無 GEX：TED 與 Flip 結構上不存在 → 流動性危機不評估；
    # VIX 14 → Gamma 危機確定不成立 → NORMAL (修正前恆為 UNKNOWN)。
    assert await _run_regime(tunnel_url="", gex={}, vix=14.0) == "NORMAL"


@pytest.mark.asyncio
async def test_regime_with_edge_but_ted_and_flip_unknown_stays_unknown() -> None:
    # 有 edge 但暫時抓不到 TED/Flip：屬於真正的未知。
    assert (
        await _run_regime(tunnel_url="http://mock-tunnel", gex={}, vix=14.0)
        == "UNKNOWN"
    )


@pytest.mark.asyncio
async def test_regime_ignores_expired_gex_cache() -> None:
    # 10 天前的 Flip 650 vs SPY 670：若採用會確定判 NORMAL；過期應視為未知。
    # （Flip 須落在大盤合理性區間內——低於現價逾 8% 會被 is_macro_gamma_flip_outlier
    # 判為離群而同樣視為未知，無法區分「過期」與「離群」兩條路徑。）
    expired = {
        "gamma_flip": 650.0,
        "_is_stale_cache": True,
        "_cache_timestamp": time.time() - 10 * 86400,
    }
    assert (
        await _run_regime(tunnel_url="http://mock-tunnel", gex=expired, vix=None)
        == "UNKNOWN"
    )
    fresh = {**expired, "_cache_timestamp": time.time() - 3600}
    assert (
        await _run_regime(tunnel_url="http://mock-tunnel", gex=fresh, vix=None)
        == "NORMAL"
    )


@pytest.mark.asyncio
async def test_fetch_gex_metrics_stale_cache_carries_timestamp() -> None:
    import config
    from market_analysis.index_microstructure import fetch_gex_metrics

    cached = {
        "data": {"spy_spot": 670.0, "gamma_flip": 660.0},
        "timestamp": 1_700_000_000.0,
    }
    with (
        patch.object(config, "TUNNEL_URL", ""),
        patch("database.cache.get_kv_cache", return_value=cached),
        patch("database.cache.save_kv_cache", new_callable=AsyncMock),
    ):
        result = await fetch_gex_metrics(allow_empty=True)
    assert result["_is_stale_cache"] is True
    assert result["_cache_timestamp"] == 1_700_000_000.0


# ===========================================================================
# 3. 事件風險快照的 Theta 門檻改以每日美元值計
# ===========================================================================
def _snapshot(total_theta: float) -> dict[str, Any]:
    from types import SimpleNamespace

    from services.event_monitor import _build_portfolio_risk_snapshot

    ctx = SimpleNamespace(
        capital=100000.0,
        risk_limit=15.0,
        total_weighted_delta=0.0,
        total_theta=total_theta,
        total_gamma=0.0,
        total_vanna=0.0,
    )
    return _build_portfolio_risk_snapshot(ctx, spy_price=670.0)


def test_event_snapshot_small_daily_theta_is_neutral() -> None:
    # 一口每日 +$3 Theta 的 short put：舊尺度 3/365≈0.008 為中性，修正單位後仍應中性。
    snap = _snapshot(3.0)
    assert snap["theta_state"] == "Theta 中性"
    assert snap["tier"] == "low"


def test_event_snapshot_large_daily_theta_is_seller_heavy() -> None:
    # 有效門檻 0.05×365 = $18.25/日
    assert _snapshot(18.0)["theta_state"] == "Theta 中性"
    snap = _snapshot(20.0)
    assert snap["theta_state"] == "賣方偏重"
    assert snap["tier"] == "medium"
    assert _snapshot(-20.0)["theta_state"] == "買方保護偏重"


# ===========================================================================
# 4. quote 的 pc/dp 為 None 時呼叫端不得拋例外
# ===========================================================================
@pytest.mark.asyncio
async def test_telemetry_pricing_with_unknown_prev_close() -> None:
    from services import order_telemetry_service as ots

    with (
        patch(
            "services.market_data_service.get_quote",
            new=AsyncMock(return_value={"c": 100.0, "pc": None, "dp": None}),
        ),
        patch(
            "market_analysis.sentiment_engine.SentimentEngine.fetch_and_calculate_iv_metrics",
            new=AsyncMock(return_value=None),
        ),
        patch(
            "market_analysis.sentiment_engine.SentimentEngine.calculate_skew",
            new=AsyncMock(return_value={}),
        ),
    ):
        limit_val, *_ = await ots.resolve_telemetry_pricing("NEWCO", "LIMIT", 10)
    assert limit_val > 0


@pytest.mark.asyncio
async def test_wti_correlated_impact_keeps_unknown_change() -> None:
    from market_analysis import wti_analysis

    with patch(
        "services.market_data_service.get_quote",
        new=AsyncMock(return_value={"c": 88.5, "pc": None, "dp": None}),
    ):
        impacts = await wti_analysis._fetch_correlated_impacts([], [])

    assert impacts, "dp 為 None 時關聯股不得被靜默丟棄"
    assert all(i.daily_change_pct is None for i in impacts)


# ===========================================================================
# 7. 交易日清單依 ET 日期快取
# ===========================================================================
def test_recent_trading_dates_cached_per_day() -> None:
    from datetime import datetime

    import market_time

    market_time._trading_dates_cache.clear()
    as_of = datetime(2026, 7, 7, 12, 0)
    real_schedule = market_time.nyse_calendar.schedule
    with patch.object(
        market_time.nyse_calendar, "schedule", side_effect=real_schedule
    ) as sched:
        first = market_time.get_recent_trading_dates(3, as_of)
        second = market_time.get_recent_trading_dates(3, as_of)
    assert first == second == ["2026-07-02", "2026-07-06", "2026-07-07"]
    assert sched.call_count == 1
    # 回傳副本：呼叫端修改不得污染快取
    first.append("x")
    assert market_time.get_recent_trading_dates(3, as_of)[-1] == "2026-07-07"


# ===========================================================================
# 8. 股息率抓不到 (None) 也短暫快取
# ===========================================================================
@pytest.mark.asyncio
async def test_dividend_yield_unknown_is_negatively_cached() -> None:
    from services.market_data_service import fundamentals

    fundamentals._dividend_yield_cache.pop("ZZZZ", None)
    fin = AsyncMock(return_value={})
    with (
        patch.object(fundamentals, "get_basic_financials", fin),
        patch(
            "services.market_data_service._core.call_yf",
            new=AsyncMock(side_effect=RuntimeError("yf down")),
        ),
    ):
        assert await fundamentals.get_dividend_yield_strict("ZZZZ") is None
        assert await fundamentals.get_dividend_yield_strict("ZZZZ") is None
    assert fin.await_count == 1
    fundamentals._dividend_yield_cache.pop("ZZZZ", None)


# ===========================================================================
# 9. 逃頂評分漏傳參數時不得捏造中性值
# ===========================================================================
def test_macro_top_escape_score_defaults_are_unknown() -> None:
    from market_analysis.index_microstructure import evaluate_macro_top_escape_score

    score, tier, _title, _factors = evaluate_macro_top_escape_score()
    assert score == 0
    assert tier == "UNKNOWN"


# ===========================================================================
# 10. IVR 未知 (None) 與真實 0.0 必須可區分
# ===========================================================================
def test_market_condition_ivr_unknown_vs_zero() -> None:
    from models.execution import MarketCondition

    base: dict[str, Any] = dict(
        vix=15.0,
        skew_percent=0.0,
        asset_price=100.0,
        ma20=100.0,
        atr_14=2.0,
        rsi_14=50.0,
    )
    assert MarketCondition(**base).ivr is None
    assert MarketCondition(**base, ivr=None).ivr is None
    assert MarketCondition(**base, ivr=float("nan")).ivr is None
    assert MarketCondition(**base, ivr=150.0).ivr is None
    assert MarketCondition(**base, ivr=0.0).ivr == 0.0


def test_ivr_overlay_zero_locks_sellers_but_unknown_does_not() -> None:
    from market_analysis.dynamic_rollover.anti_washout import (
        apply_ivr_strategy_overlay_impl,
    )
    from market_analysis.ivr_strategy_gate import is_selling_locked_by_ivr

    locked = apply_ivr_strategy_overlay_impl(is_selling_locked_by_ivr, "CSP", "", 0.0)
    assert "賣方策略已鎖死" in locked
    assert (
        apply_ivr_strategy_overlay_impl(is_selling_locked_by_ivr, "CSP", "", None)
        == "CSP"
    )


def test_execution_router_unknown_ivr_does_not_lock() -> None:
    from models.execution import MarketCondition
    from services.execution_router import ExecutionRouter

    cond = MarketCondition(
        vix=15.0,
        skew_percent=0.0,
        asset_price=100.0,
        ma20=95.0,
        atr_14=2.0,
        rsi_14=55.0,
        uoa_detected=False,
        relative_strength=1.0,
    )
    decision = ExecutionRouter().evaluate_market(cond)
    reason = str(getattr(decision, "trigger_reason", ""))
    assert "IVR 極低位硬鎖" not in reason


def test_strategy_signal_unknown_ivr_does_not_force_itm_call() -> None:
    from market_analysis.strategy.indicators import _determine_strategy_signal

    ind = {
        "price": 100.0,
        "rsi": 30.0,
        "hv_rank": 40.0,
        "sma20": 100.0,
        "macd_hist": 0.0,
    }
    assert _determine_strategy_signal(ind, ivr=None)[0] == "STO_PUT"
    assert _determine_strategy_signal(ind, ivr=0.0)[0] == "BTO_CALL"
