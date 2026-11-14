"""NRO 15 分鐘掃描策略對齊閘門與 PSQ 戰情分支退役測試。"""

from types import SimpleNamespace
from typing import Any, Dict, List, Optional
from unittest.mock import AsyncMock, MagicMock, patch

import pandas as pd
import pytest

from market_analysis.strategy.indicators import STO_CALL_OVERBOUGHT_RSI
from services.trading_service import TradingService
from services.trading_service.market_scan import (
    _strategy_alignment_reject_reason,
)

REGIME_V = "REGIME_V_BREAKDOWN_CHASE"


# --- 純函式閘門 -------------------------------------------------------------


def test_sto_call_rejected_when_overbought() -> None:
    reason = _strategy_alignment_reject_reason(
        "STO_CALL", STO_CALL_OVERBOUGHT_RSI + 5, 500, "RIGHT_SIDE", None
    )
    assert reason is not None
    # 邊界值 (RSI == 65) 不屬追強，放行
    assert (
        _strategy_alignment_reject_reason(
            "STO_CALL", STO_CALL_OVERBOUGHT_RSI, 500, "RIGHT_SIDE", None
        )
        is None
    )
    # RSI 缺值 fail-closed
    assert (
        _strategy_alignment_reject_reason("STO_CALL", None, 500, "RIGHT_SIDE", None)
        is not None
    )


@pytest.mark.parametrize("shares", [0, 50, 99.0])
def test_sto_call_rejected_without_shares(shares: float) -> None:
    assert (
        _strategy_alignment_reject_reason("STO_CALL", 45, shares, "RIGHT_SIDE", None)
        is not None
    )


def test_sto_call_allowed_with_100_shares() -> None:
    assert (
        _strategy_alignment_reject_reason("STO_CALL", 45, 100, "RIGHT_SIDE", None)
        is None
    )


@pytest.mark.parametrize("mode", ["RIGHT_SIDE", "LEFT_SIDE", "", "UNKNOWN"])
def test_bto_put_rejected_for_non_short_modes(mode: str) -> None:
    assert _strategy_alignment_reject_reason("BTO_PUT", 45, 0, mode, None) is not None


def test_bto_put_allowed_for_short_side() -> None:
    assert (
        _strategy_alignment_reject_reason("BTO_PUT", 45, 0, "SHORT_SIDE", None) is None
    )


def test_bto_put_dynamic_requires_regime_v() -> None:
    assert (
        _strategy_alignment_reject_reason("BTO_PUT", 45, 0, "DYNAMIC", REGIME_V) is None
    )
    assert (
        _strategy_alignment_reject_reason(
            "BTO_PUT", 45, 0, "DYNAMIC", "REGIME_II_CHAOS_STANDASIDE"
        )
        is not None
    )
    assert (
        _strategy_alignment_reject_reason("BTO_PUT", 45, 0, "DYNAMIC", None) is not None
    )


@pytest.mark.parametrize("strategy", ["STO_PUT", "BTO_CALL"])
@pytest.mark.parametrize("mode", ["RIGHT_SIDE", "LEFT_SIDE", "SHORT_SIDE", "DYNAMIC"])
def test_other_strategies_unaffected(strategy: str, mode: str) -> None:
    assert _strategy_alignment_reject_reason(strategy, 70, 0, mode, None) is None


# --- run_market_scan 整合 ---------------------------------------------------


async def _run_scan(
    *,
    strategy: str,
    rsi: float = 45.0,
    quantity: Optional[float] = 250.0,
    trading_strategy: str = "RIGHT_SIDE",
    enable_psq: bool = False,
    regime: Any = None,
    suggested_contracts: int = 5,
    psq_result: Any = None,
) -> Dict[str, Any]:
    ts = TradingService(MagicMock())
    holding: Dict[str, Any] = {"user_id": 1, "symbol": "AAPL", "avg_cost": 150.0}
    if quantity is not None:
        holding["quantity"] = quantity

    spy_df = pd.DataFrame(
        {"Close": [670.0]}, index=pd.date_range("2026-05-20", periods=1)
    )
    hist_df = pd.DataFrame(
        {
            "Open": [150.0] * 30,
            "High": [152.0] * 30,
            "Low": [148.0] * 30,
            "Close": [150.0] * 30,
            "Volume": [1000] * 30,
        },
        index=pd.date_range("2026-05-20", periods=30),
    )
    option_res: Dict[str, Any] = {
        "symbol": "AAPL",
        "stock_cost": 150.0,
        "strategy": strategy,
        "weighted_delta": 0.2,
        "iv": 0.25,
        "price": 150.0,
        "rsi": rsi,
    }
    if psq_result is not None:
        option_res["psq_result"] = psq_result

    ctx = MagicMock()
    ctx.capital = 100000.0
    ctx.total_weighted_delta = 0.0
    ctx.option_alert_mode = 1
    ctx.risk_limit = 15.0
    ctx.last_rehedge_alert_time = 0
    ctx.enable_psq_watchlist = enable_psq
    ctx.trading_strategy = trading_strategy

    opt_res = SimpleNamespace(
        suggested_contracts=suggested_contracts,
        suggested_hedge_spy=0.0,
        warnings=[],
    )
    upsert = MagicMock()
    regime_mock = regime if regime is not None else AsyncMock()
    with patch("database.get_all_watchlist", return_value=[(1, "AAPL", 1)]), patch(
        "database.holdings.get_all_holdings", return_value=[holding]
    ), patch(
        "services.market_data_service.get_spy_history_df",
        new_callable=AsyncMock,
        return_value=spy_df,
    ), patch(
        "services.market_data_service.get_macro_environment",
        new_callable=AsyncMock,
        return_value={"vix": 18.0, "oil": 75.0, "vix_change": 0.0},
    ), patch(
        "services.market_data_service.get_history_df",
        new_callable=AsyncMock,
        return_value=hist_df,
    ), patch(
        "market_math.analyze_symbol",
        new_callable=AsyncMock,
        return_value=option_res,
    ), patch(
        "market_analysis.sentiment_engine.SentimentEngine.calculate_skew",
        new_callable=AsyncMock,
        return_value={"skew": 1.0},
    ), patch(
        "market_analysis.sentiment_engine.SentimentEngine.calculate_pcr",
        new_callable=AsyncMock,
        return_value={"pcr": 0.85},
    ), patch(
        "services.calendar_service.calendar_service.get_symbol_earnings",
        new_callable=AsyncMock,
        return_value=MagicMock(tte_hours=48),
    ), patch(
        "services.news_service.fetch_recent_news",
        new_callable=AsyncMock,
        return_value="",
    ), patch("database.upsert_user_config", upsert), patch(
        "database.get_full_user_context", return_value=ctx
    ), patch(
        "market_analysis.portfolio.refresh_portfolio_greeks",
        new_callable=AsyncMock,
    ), patch(
        "market_analysis.risk_engine.load_available_collateral",
        new_callable=AsyncMock,
        return_value=(1_000_000.0, None),
    ), patch(
        "services.trading_service.market_scan.optimize_position_risk",
        return_value=opt_res,
    ), patch.object(ts, "_validate_trade_pipeline", return_value=(True, "")), patch(
        "services.trading_service.market_scan._classify_symbol_regime_value",
        regime_mock,
    ), patch(
        "market_analysis.hedging.evaluate_rehedge_necessity",
        return_value={"action": "REHEDGE"},
    ), patch(
        "market_analysis.hedging.get_tuned_risk_advice",
        side_effect=lambda _uid, advice: advice,
    ):
        res = await ts.run_market_scan(is_auto=True)
    return {"res": res, "upsert": upsert, "regime": regime_mock}


def _alerts(out: Dict[str, Any]) -> List[Dict[str, Any]]:
    alerts: List[Dict[str, Any]] = out["res"].get(1, [])
    return alerts


@pytest.mark.asyncio
async def test_scan_sto_call_rejected_when_overbought() -> None:
    out = await _run_scan(strategy="STO_CALL", rsi=72.0)
    assert _alerts(out) == []


@pytest.mark.asyncio
@pytest.mark.parametrize("quantity", [0.0, 50.0, None])
async def test_sto_call_rejected_without_shares_scan(quantity: Optional[float]) -> None:
    out = await _run_scan(strategy="STO_CALL", quantity=quantity)
    assert _alerts(out) == []


@pytest.mark.asyncio
async def test_sto_call_capped_by_shares() -> None:
    out = await _run_scan(strategy="STO_CALL", quantity=250.0, suggested_contracts=5)
    alerts = _alerts(out)
    assert len(alerts) == 1
    assert alerts[0]["safe_qty"] == 2


@pytest.mark.asyncio
async def test_bto_put_rejected_for_right_and_left_side() -> None:
    for mode in ("RIGHT_SIDE", "LEFT_SIDE", ""):
        out = await _run_scan(strategy="BTO_PUT", trading_strategy=mode)
        assert _alerts(out) == [], mode


@pytest.mark.asyncio
async def test_bto_put_allowed_for_short_side_scan() -> None:
    out = await _run_scan(strategy="BTO_PUT", trading_strategy="SHORT_SIDE")
    assert len(_alerts(out)) == 1


@pytest.mark.asyncio
async def test_bto_put_dynamic_regime_v_allowed_scan() -> None:
    out = await _run_scan(
        strategy="BTO_PUT",
        trading_strategy="DYNAMIC",
        regime=AsyncMock(return_value=REGIME_V),
    )
    assert len(_alerts(out)) == 1
    out = await _run_scan(
        strategy="BTO_PUT",
        trading_strategy="DYNAMIC",
        regime=AsyncMock(return_value="REGIME_II_CHAOS_STANDASIDE"),
    )
    assert _alerts(out) == []


@pytest.mark.asyncio
async def test_bto_put_dynamic_fail_closed() -> None:
    """分類器丟例外 (未 mock helper) 時一律拒絕。"""
    from services.trading_service.market_scan import _classify_symbol_regime_value

    with patch(
        "market_analysis.index_microstructure.fetch_symbol_gex_metrics",
        new_callable=AsyncMock,
        side_effect=RuntimeError("edge down"),
    ):
        assert await _classify_symbol_regime_value("AAPL", 150.0) is None
    with patch(
        "market_analysis.index_microstructure.fetch_symbol_gex_metrics",
        new_callable=AsyncMock,
        return_value={"put_wall": 140.0, "net_gex": 1.0},
    ), patch(
        "market_analysis.dynamic_rollover.regime_classifier.classify_dynamic_regime",
        new_callable=AsyncMock,
        side_effect=RuntimeError("boom"),
    ):
        assert await _classify_symbol_regime_value("AAPL", 150.0) is None

    # 整合：helper 回 None -> 不推 BTO_PUT
    out = await _run_scan(
        strategy="BTO_PUT",
        trading_strategy="DYNAMIC",
        regime=AsyncMock(return_value=None),
    )
    assert _alerts(out) == []


@pytest.mark.asyncio
async def test_dynamic_regime_classified_once_per_symbol() -> None:
    regime = AsyncMock(return_value=REGIME_V)
    out = await _run_scan(strategy="BTO_PUT", trading_strategy="DYNAMIC", regime=regime)
    assert regime.await_count == 1
    assert len(_alerts(out)) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("strategy", ["STO_PUT", "BTO_CALL"])
async def test_sto_put_and_bto_call_unaffected(strategy: str) -> None:
    out = await _run_scan(
        strategy=strategy, rsi=70.0, quantity=0.0, trading_strategy="RIGHT_SIDE"
    )
    assert len(_alerts(out)) == 1


@pytest.mark.asyncio
async def test_reject_before_rehedge_write() -> None:
    out = await _run_scan(strategy="STO_CALL", rsi=72.0)
    assert _alerts(out) == []
    out["upsert"].assert_not_called()
    out = await _run_scan(strategy="BTO_PUT", trading_strategy="RIGHT_SIDE")
    assert _alerts(out) == []
    out["upsert"].assert_not_called()


@pytest.mark.asyncio
async def test_no_psq_alerts() -> None:
    """即使 enable_psq_watchlist=True 且標的帶 PSQ 訊號，也不產生 PSQ 警報。"""
    psq = SimpleNamespace(is_breakout_long=True, is_near_support=True)
    out = await _run_scan(strategy="", enable_psq=True, psq_result=psq)
    assert all(a.get("alert_type") != "PSQ" for a in _alerts(out))
    out = await _run_scan(
        strategy="BTO_CALL", enable_psq=True, psq_result=psq, quantity=0.0
    )
    assert all(a.get("alert_type") != "PSQ" for a in _alerts(out))
