"""單元測試：管理層前瞻指引邊際變動與態度語意模組 (guidance_delta.py)。"""

from __future__ import annotations

from market_analysis.fundamental_pipeline.guidance_delta import (
    calculate_extraction_confidence,
    calculate_management_tone_score,
    calculate_tone_delta,
    compare_guidance,
    evaluate_margin_trend,
    is_magnitude_comparable,
)
from market_analysis.fundamental_pipeline.models import (
    GuidanceExtraction,
    MarginDirection,
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
    target: str | None = "FY2026",
) -> GuidanceExtraction:
    return GuidanceExtraction(
        symbol=symbol,
        fiscal_period=fiscal_period,
        guidance_target_period=target,
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
    # 無前期可比時為 None，不得以當期分數冒充邊際變化
    assert calculate_tone_delta(40.0, None) is None


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
    assert "調升" in summary_raised.summary_text

    # 營收與 EPS 明顯調降 (-5% 與 -10%)
    curr_lowered = _make_sample_guidance(revenue=19000000000.0, eps=0.72)
    summary_lowered = compare_guidance(curr_lowered, prior)
    assert summary_lowered.verdict == "LOWERED"
    assert summary_lowered.revenue_guidance_delta_pct == -0.05
    assert "調降" in summary_lowered.summary_text


def test_compare_guidance_maintained() -> None:
    """測試微幅變動判定為 MAINTAINED。"""
    prior = _make_sample_guidance(revenue=20000000000.0, eps=0.80)
    curr_flat = _make_sample_guidance(revenue=20010000000.0, eps=0.80)
    summary_flat = compare_guidance(curr_flat, prior)
    assert summary_flat.verdict == "MAINTAINED"


def test_compare_guidance_without_numbers_is_unknown() -> None:
    """無數值指引時依 docs §2.5 一律 UNKNOWN，不以語意態度推論調升 / 調降。"""
    curr_tone_high = _make_sample_guidance(
        revenue=None,
        eps=None,
        backlog=2,
        pricing=2,
        supply=1,
        defensive=1,
    )
    summary = compare_guidance(curr_tone_high, None)
    assert summary.verdict == "UNKNOWN"
    assert summary.tone_delta is None
    assert summary.comparison_note == "無同期別前後數值指引"

    curr_tone_low = _make_sample_guidance(
        revenue=None,
        eps=None,
        backlog=-2,
        pricing=-2,
        supply=-2,
        defensive=-2,
        margins=[MarginGuidance(metric_name="Gross Margin", direction="COMPRESSING")],
    )
    assert compare_guidance(curr_tone_low, None).verdict == "UNKNOWN"


def test_compare_guidance_numbers_without_prior_is_unknown() -> None:
    """只有當期數值、沒有前期指引也沒有共識時無從比較，回傳 UNKNOWN。"""
    curr = _make_sample_guidance(revenue=21000000000.0, eps=0.85)
    summary = compare_guidance(curr, None)
    assert summary.verdict == "UNKNOWN"
    assert summary.revenue_guidance_delta_pct is None
    assert summary.eps_guidance_delta_pct is None


def test_compare_guidance_different_target_period_not_compared() -> None:
    """前後指引目標期別不同（下季 vs 上一季的下季）時不做數值比較。"""
    prior = _make_sample_guidance(revenue=20000000000.0, eps=0.80, target="2026-Q3")
    curr = _make_sample_guidance(revenue=25000000000.0, eps=1.00, target="Q4 2026")
    summary = compare_guidance(curr, prior)
    assert summary.verdict == "UNKNOWN"
    assert summary.revenue_guidance_delta_pct is None
    assert "目標期別不同" in summary.comparison_note
    # 態度邊際仍可計算（有前期）
    assert summary.tone_delta == 0.0


def test_compare_guidance_same_target_with_format_variants() -> None:
    """目標期別格式不同但正規化後相同（FY2026 / fiscal year 2026）時可比較。"""
    prior = _make_sample_guidance(revenue=20000000000.0, eps=0.80, target="FY2026")
    curr = _make_sample_guidance(
        revenue=21000000000.0, eps=0.85, target="fiscal year 2026"
    )
    assert compare_guidance(curr, prior).verdict == "RAISED"


def test_compare_guidance_unit_mismatch_not_compared() -> None:
    """一筆以完整美元、一筆以十億美元填寫（差距逾 100 倍）時判為無法比較。"""
    prior = _make_sample_guidance(revenue=20.0, eps=None)  # 20 (billion)
    curr = _make_sample_guidance(revenue=21000000000.0, eps=None)
    summary = compare_guidance(curr, prior)
    assert summary.verdict == "UNKNOWN"
    assert summary.revenue_guidance_delta_pct is None
    assert "單位不一致" in summary.comparison_note


def test_is_magnitude_comparable() -> None:
    assert is_magnitude_comparable(21e9, 20e9)
    assert not is_magnitude_comparable(21e9, 21.0)
    assert is_magnitude_comparable(0.85, 0.80, 0.05)
    # EPS 接近零時以下限計算，避免 0.01 → 0.02 被誤判為單位不一致
    assert is_magnitude_comparable(0.02, 0.01, 0.05)
    assert not is_magnitude_comparable(85.0, 0.80, 0.05)


def test_calculate_extraction_confidence() -> None:
    """信心分數 = (可溯源態度維度數 + 營收 / EPS / 利潤率指引之有無) / 7。"""
    full = _make_sample_guidance(
        margins=[MarginGuidance(metric_name="Gross Margin", direction="FLAT")]
    )
    assert calculate_extraction_confidence(full, 4) == 1.0
    bare = _make_sample_guidance(revenue=None, eps=None)
    assert calculate_extraction_confidence(bare, 0) == 0.0
    assert calculate_extraction_confidence(bare, 2) == round(2 / 7, 4)
    # 超出範圍之溯源數被截斷
    assert calculate_extraction_confidence(bare, 9) == round(4 / 7, 4)


def test_margin_trend_labels_are_traditional_chinese() -> None:
    """利潤率趨勢文字不得夾雜英文。"""
    directions: tuple[MarginDirection, ...] = ("EXPANDING", "COMPRESSING", "FLAT")
    for direction in directions:
        g = _make_sample_guidance(
            margins=[MarginGuidance(metric_name="Gross Margin", direction=direction)]
        )
        trend = evaluate_margin_trend(g)
        assert trend.startswith("利潤率")
        assert not any(ch.isascii() and ch.isalpha() for ch in trend)


def test_compare_guidance_conflicting_signals() -> None:
    """測試營收調升但 EPS 調降之分歧訊號仲裁。"""
    prior = _make_sample_guidance(revenue=20000000000.0, eps=1.00)

    # 營收 +5%, EPS -10%, 但態度極度謹慎且利潤率承壓 -> LOWERED
    curr_mixed_pessimistic = _make_sample_guidance(
        revenue=21000000000.0,
        eps=0.90,
        backlog=-1,
        pricing=-1,
        supply=-1,
        defensive=-1,
        margins=[MarginGuidance(metric_name="Gross Margin", direction="COMPRESSING")],
    )
    summary_lowered = compare_guidance(curr_mixed_pessimistic, prior)
    assert summary_lowered.verdict == "LOWERED"

    # 營收 +5%, EPS -10%, 但態度強烈擴張且利潤率擴張 -> RAISED
    curr_mixed_optimistic = _make_sample_guidance(
        revenue=21000000000.0,
        eps=0.90,
        backlog=2,
        pricing=2,
        supply=1,
        defensive=1,
        margins=[MarginGuidance(metric_name="Gross Margin", direction="EXPANDING")],
    )
    summary_raised = compare_guidance(curr_mixed_optimistic, prior)
    assert summary_raised.verdict == "RAISED"


def test_guidance_extraction_null_margin_coercion() -> None:
    """測試當 LLM 回傳 margin_guidance: null 時模型自動轉為空陣列。"""
    json_text = """{
        "symbol": "TSLA",
        "fiscal_period": "2026-Q3",
        "revenue_guidance_midpoint_usd": 25000000000.0,
        "eps_guidance_midpoint_usd": 0.85,
        "margin_guidance": null,
        "backlog_tone": {"score": 1, "quote_snippet": "需求穩定"},
        "pricing_power_tone": {"score": 1, "quote_snippet": "價格良好"},
        "supply_chain_tone": {"score": 0, "quote_snippet": "交付正常"},
        "defensive_posture_tone": {"score": 0, "quote_snippet": "無"},
        "reasoning_traditional_chinese": "管理層指引維持健康。"
    }"""
    g = GuidanceExtraction.model_validate_json(json_text)
    assert g.margin_guidance == []
    assert g.symbol == "TSLA"
