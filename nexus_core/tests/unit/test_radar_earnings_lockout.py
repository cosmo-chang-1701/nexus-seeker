"""雷達／SDDM 路徑：財報 14 日內不得再建議賣方收租。

/x 的「情緒背離偵測」已接財報鎖定（PR #230）；雷達的背離閘門
（evaluation.py）與 Skew 規則判讀（skew_commentary.py）原本仍會輸出
「僅允許小倉位收租」「收租主動路由」「偏多／賣方收租」，與事件日曆的禁賣打架。
"""

from typing import Any, Optional
from unittest.mock import AsyncMock, patch

import pytest

from market_analysis.intraday_pipeline import build_watchlist_skew_rule_commentary
from market_analysis.intraday_pipeline.events import (
    EARNINGS_LOCKOUT_HOURS,
    is_earnings_lockout,
)
from models.schemas import (
    EnhancedWatchlistMetrics,
    WatchlistEventContext,
    WatchlistTacticalPlan,
)

OLD_INCOME = "僅允許小倉位收租"


def _event(tte: Optional[float]) -> WatchlistEventContext:
    return WatchlistEventContext(
        earnings_date="2026-11-17" if tte is not None else None,
        earnings_tte_hours=tte,
        risk_mode="normal",
        summary="測試",
    )


def _metrics(**overrides: Any) -> EnhancedWatchlistMetrics:
    payload: dict[str, Any] = dict(
        symbol="TEST",
        exchange="NASDAQ",
        current_price=100.0,
        buy_zone_status="Wait",
        buy_price_phase1=90.0,
        buy_price_phase2=80.0,
        buy_price_phase3=70.0,
        sell_zone_status="Wait",
        sell_price_phase1=110.0,
        sell_price_phase2=120.0,
        sell_price_phase3=130.0,
        rsi_14=50.0,
        atr_14=2.0,
        beta=1.0,
        ma20=100.0,
        ma50=100.0,
        ma200=100.0,
        iv_rank=40.0,
        iv_percentile=40.0,
        option_skew=0.0,
        skew_percentile=50.0,
        option_skew_state="正常",
        pcr=1.0,
        volume_poc=100.0,
        gex_max_put_wall=100.0,
        vanna_sensitivity=0.1,
        relative_strength_spy=1.0,
    )
    payload.update(overrides)
    return EnhancedWatchlistMetrics(**payload)


def _tactical() -> WatchlistTacticalPlan:
    return WatchlistTacticalPlan(
        scenario="wait",
        sddm_route="WAIT (正常)",
        action_guideline="指引",
        dynamic_grid_step=1.0,
        hidden_delta_risk=0.0,
        hedge_instruction=None,
        hedge_allocation_shares=0,
        alert_level="yellow",
    )


# --- is_earnings_lockout ---------------------------------------------------


@pytest.mark.parametrize(
    ("tte", "expected"),
    [
        (None, False),
        (-3.0, False),  # 已公布
        (0.0, False),
        (103.2, True),  # 4.3 天
        (EARNINGS_LOCKOUT_HOURS, True),
        (EARNINGS_LOCKOUT_HOURS + 1.0, False),
    ],
)
def test_is_earnings_lockout_window(tte: Optional[float], expected: bool) -> None:
    assert is_earnings_lockout(_event(tte)) is expected


def test_is_earnings_lockout_none_context() -> None:
    assert is_earnings_lockout(None) is False


# --- skew_commentary ----------------------------------------------------------


def test_iv_volcano_harvest_route_blocked_before_earnings() -> None:
    m = _metrics(pcr=0.8, skew_percentile=95.0, iv_rank=85.0, option_skew=6.0)
    locked = build_watchlist_skew_rule_commentary(m, None, event_context=_event(103.2))
    assert "收租主動路由" not in locked
    assert "財報鎖定／禁收租" in locked
    assert "嚴禁賣方收租" in locked
    # 窗口外原行為不變
    free = build_watchlist_skew_rule_commentary(m, None, event_context=_event(500.0))
    assert "收租主動路由" in free
    assert free == build_watchlist_skew_rule_commentary(m, None)


def test_bullish_badge_drops_income_before_earnings() -> None:
    m = _metrics(pcr=0.8, skew_percentile=10.0, option_skew=-4.0)
    locked = build_watchlist_skew_rule_commentary(m, None, event_context=_event(103.2))
    assert "偏多／賣方收租" not in locked
    assert "偏多（財報前禁收租）" in locked
    assert "偏多／賣方收租" in build_watchlist_skew_rule_commentary(m, None)


# --- evaluation.py 背離閘門 --------------------------------------------------


async def _evaluate(
    metrics: EnhancedWatchlistMetrics, event: WatchlistEventContext, dp: float
) -> Any:
    from market_analysis.intraday_pipeline import evaluate_watchlist_symbol

    with patch(
        "market_analysis.intraday_pipeline.build_enhanced_watchlist_metrics",
        new_callable=AsyncMock,
        return_value=metrics,
    ), patch(
        "market_analysis.intraday_pipeline.build_watchlist_event_context",
        new_callable=AsyncMock,
        return_value=event,
    ), patch(
        "market_analysis.intraday_pipeline.WatchlistRiskController.process_metrics",
        return_value=_tactical(),
    ), patch(
        "database.market_cache.get_fundamental_cache", return_value=None
    ), patch(
        "market_analysis.index_microstructure.get_market_regime",
        new_callable=AsyncMock,
        return_value="NORMAL",
    ), patch(
        "market_analysis.index_microstructure.fetch_symbol_gex_metrics",
        new_callable=AsyncMock,
        return_value=None,
    ), patch(
        "services.market_data_service.get_quote",
        new_callable=AsyncMock,
        return_value={"dp": dp},
    ):  # fmt: skip
        return await evaluate_watchlist_symbol("TEST")


@pytest.mark.asyncio
async def test_structural_divergence_gate_blocks_income_before_earnings() -> None:
    # Skew 低分位（Call 貴）＋ PCR > 1.5
    m = _metrics(skew_percentile=5.0, pcr=1.8)
    locked = await _evaluate(m, _event(103.2), dp=0.0)
    assert "結構性情緒背離" in locked.tactical.action_guideline
    assert OLD_INCOME not in locked.tactical.action_guideline
    assert "嚴禁追價買權與賣方收租" in locked.tactical.action_guideline

    free = await _evaluate(m, _event(None), dp=0.0)
    assert (
        "建議停止追價單腿，僅允許小倉位收租並搭配保護性 Put/Collar 或使用價差結構。"
        in free.tactical.action_guideline
    )


@pytest.mark.asyncio
async def test_iv_suppression_gate_blocks_income_before_earnings() -> None:
    m = _metrics(iv_rank=10.0, iv_percentile=10.0)
    locked = await _evaluate(m, _event(103.2), dp=-5.0)
    assert "IV Suppression Divergence" in locked.tactical.action_guideline
    assert OLD_INCOME not in locked.tactical.action_guideline
    assert "財報鎖定窗口內嚴禁賣方收租" in locked.tactical.action_guideline

    free = await _evaluate(m, _event(None), dp=-5.0)
    assert "僅允許小倉位收租並搭配保護性結構。" in free.tactical.action_guideline
