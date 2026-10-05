"""單元測試：管理層前瞻指引邊際變動與態度語意模組 (guidance_delta.py)。"""

from __future__ import annotations

from market_analysis.fundamental_pipeline.guidance_delta import (
    calculate_management_tone_score,
    calculate_tone_delta,
    compare_guidance,
    evaluate_margin_trend,
)
from market_analysis.fundamental_pipeline.models import (
    GuidanceExtraction,
    MarginGuidance,
    ToneMetric,
)


def _make_sample_guidance(
    symbol: str = "TSLA",
    fiscal_period: str = "2026-Q3",
    revenue: float | None = 25000000000.0,
    eps: float | None = 0.85,
    backlog: int = 1,
    pricing: int = 1,
    supply: int = 0,
    defensive: int = 0,
    margins: list[MarginGuidance] | None = None,
) -> GuidanceExtraction:
    return GuidanceExtraction(
        symbol=symbol,
        fiscal_period=fiscal_period,
        revenue_guidance_midpoint_usd=revenue,
        eps_guidance_midpoint_usd=eps,
        margin_guidance=margins or [],
        backlog_tone=ToneMetric(score=backlog, quote_snippet="需求強勁"),
        pricing_power_tone=ToneMetric(score=pricing, quote_snippet="定價持穩"),
        supply_chain_tone=ToneMetric(score=supply, quote_snippet="交付正常"),
        defensive_posture_tone=ToneMetric(score=defensive, quote_snippet="正常營運"),
        reasoning_traditional_chinese="管理層維持擴張節奏。",
    )


def test_calculate_management_tone_score_extremes() -> None:
    """測試管理層態度語意評分在極端值情況下的映射。"""
    # 全滿分 +2 -> +100.0
    g_max = _make_sample_guidance(backlog=2, pricing=2, supply=2, defensive=2)
    score_max = calculate_management_tone_score(g_max)
    assert score_max == 100.0

    # 全最低分 -2 -> -100.0
    g_min = _make_sample_guidance(backlog=-2, pricing=-2, supply=-2, defensive=-2)
    score_min = calculate_management_tone_score(g_min)
    assert score_min == -100.0

    # 全中性 0 -> 0.0
    g_zero = _make_sample_guidance(backlog=0, pricing=0, supply=0, defensive=0)
    score_zero = calculate_management_tone_score(g_zero)
    assert score_zero == 0.0


def test_calculate_tone_delta() -> None:
    """測試相較前期的態度邊際變化分數。"""
    assert calculate_tone_delta(50.0, 20.0) == 30.0
    assert calculate_tone_delta(-30.0, 0.0) == -30.0
    assert calculate_tone_delta(40.0, None) == 40.0


def test_evaluate_margin_trend() -> None:
    """測試毛利率與營益率擴張或壓縮分類。"""
    g_empty = _make_sample_guidance(margins=[])
    assert evaluate_margin_trend(g_empty) == "無明示指引"

    g_expand = _make_sample_guidance(
        margins=[
            MarginGuidance(metric_name="Gross Margin", direction="EXPANDING"),
            MarginGuidance(metric_name="Operating Margin", direction="FLAT"),
        ]
    )
    assert "利潤率擴張" in evaluate_margin_trend(g_expand)

    g_compress = _make_sample_guidance(
        margins=[
            MarginGuidance(metric_name="Gross Margin", direction="COMPRESSING"),
        ]
    )
    assert "利潤率承壓" in evaluate_margin_trend(g_compress)


def test_compare_guidance_raised_and_lowered() -> None:
    """測試前瞻指引邊際變動仲裁 (RAISED 與 LOWERED)。"""
    prior = _make_sample_guidance(revenue=20000000000.0, eps=0.80)
    # 營收與 EPS 明顯調升 (+5% 與 +6%)
    curr_raised = _make_sample_guidance(revenue=21000000000.0, eps=0.85)
    summary_raised = compare_guidance(curr_raised, prior)
    assert summary_raised.verdict == "RAISED"
    assert summary_raised.revenue_guidance_delta_pct == 0.05
    assert summary_raised.eps_guidance_delta_pct is not None
    assert summary_raised.eps_guidance_delta_pct > 0.05
    assert "RAISED" in summary_raised.summary_text

    # 營收與 EPS 明顯調降 (-5% 與 -10%)
    curr_lowered = _make_sample_guidance(revenue=19000000000.0, eps=0.72)
    summary_lowered = compare_guidance(curr_lowered, prior)
    assert summary_lowered.verdict == "LOWERED"
    assert summary_lowered.revenue_guidance_delta_pct == -0.05
    assert "LOWERED" in summary_lowered.summary_text


def test_compare_guidance_maintained() -> None:
    """測試微幅變動判定為 MAINTAINED。"""
    prior = _make_sample_guidance(revenue=20000000000.0, eps=0.80)
    curr_flat = _make_sample_guidance(revenue=20010000000.0, eps=0.80)
    summary_flat = compare_guidance(curr_flat, prior)
    assert summary_flat.verdict == "MAINTAINED"


def test_compare_guidance_fallback_to_tone() -> None:
    """測試無數值指引時退回語意與利潤率判定。"""
    curr_tone_high = _make_sample_guidance(
        revenue=None,
        eps=None,
        backlog=2,
        pricing=2,
        supply=1,
        defensive=1,
    )
    summary = compare_guidance(curr_tone_high, None)
    assert summary.verdict == "RAISED"
