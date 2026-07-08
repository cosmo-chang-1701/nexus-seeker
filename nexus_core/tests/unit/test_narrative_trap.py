import pytest
from market_analysis.insights_engine import InsightsEngine, RiskInsightsContext


@pytest.mark.asyncio
async def test_narrative_trap_override():
    context = RiskInsightsContext(
        symbol="SPY",
        current_price=254.30,
        put_wall=250.00,
        net_gex_status="NEGATIVE_GAMMA_ZONE",
        term_structure=1.0,
        uoa_institutional_short_call=False,
        iv_rank=0.5,
        max_pain_deviation_pct=0.0912,
        can_trade_spreads=True,
        cash_reserve_protection=True,
        expected_move_lower=240.0,
        has_positive_gamma_support=False,
        cb_triggered=False,
    )
    dmp_label, status_label, suggestion = InsightsEngine.generate_cro_insight(context)
    assert "磁吸回升" not in (status_label or "")
    assert "底牆保衛" in (status_label or "") or "嚴防破位" in (status_label or "")
