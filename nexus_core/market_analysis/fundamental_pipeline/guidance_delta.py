"""管理層前瞻指引邊際變動與態度語意評估模型 (Guidance Delta & Management Tone)。

職責：
1. 計算管理層態度綜合語意評分 (Management Tone Score)。
2. 計算態度邊際變更 (Tone Delta)。
3. 比對當期前瞻指引與前期指引 (或分析師共識)，計算營收與 EPS 邊際變化百分比。
4. 分析毛利率與營益率擴張/承壓趨勢 (Margin Guidance)。
5. 仲裁前瞻指引綜合裁決 (RAISED / LOWERED / MAINTAINED / UNKNOWN)。
"""

from __future__ import annotations

import logging

from market_analysis.fundamental_pipeline.earnings_surprise import FLOOR_EPS
from market_analysis.fundamental_pipeline.fiscal_period import (
    normalize_guidance_period,
)
from market_analysis.fundamental_pipeline.models import (
    GuidanceDeltaSummary,
    GuidanceExtraction,
    GuidanceVerdict,
)

logger = logging.getLogger(__name__)

# ============================================================================
# 語意權重與門檻常數
# ============================================================================

WEIGHT_BACKLOG: float = 0.25
WEIGHT_PRICING_POWER: float = 0.25
WEIGHT_SUPPLY_CHAIN: float = 0.25
WEIGHT_DEFENSIVE_POSTURE: float = 0.25

TONE_SCORE_CLIP: float = 100.0
RAISE_THRESHOLD_PCT: float = 0.01  # +1% 以上視為調升指引
LOWER_THRESHOLD_PCT: float = -0.01  # -1% 以下視為調降指引
MAGNITUDE_MISMATCH_RATIO: float = 100.0  # 前後數值差距逾 100 倍視為單位不一致，不可比較
CONFIDENCE_EVIDENCE_ITEMS: float = 7.0  # 信心分數之證據項總數（4 維態度 + 3 項數值）


def calculate_management_tone_score(
    extraction: GuidanceExtraction,
    weight_backlog: float = WEIGHT_BACKLOG,
    weight_pricing: float = WEIGHT_PRICING_POWER,
    weight_supply: float = WEIGHT_SUPPLY_CHAIN,
    weight_defensive: float = WEIGHT_DEFENSIVE_POSTURE,
) -> float:
    """計算管理層態度綜合語意分數，範圍正規化至 [-100.0, +100.0]。

    每項維度評分介於 [-2, +2]，其中：
    - +2 代表定價自信、強勁擴張、供需順暢、進取擴張
    - -2 代表極度惡化、防禦緊縮、供應鏈受阻、謹慎防禦
    """
    total_weight = weight_backlog + weight_pricing + weight_supply + weight_defensive
    if total_weight <= 0:
        return 0.0

    weighted_score = (
        extraction.backlog_tone.score * weight_backlog
        + extraction.pricing_power_tone.score * weight_pricing
        + extraction.supply_chain_tone.score * weight_supply
        + extraction.defensive_posture_tone.score * weight_defensive
    ) / total_weight

    # 將 [-2.0, +2.0] 線性映射至 [-100.0, +100.0]
    normalized = (weighted_score / 2.0) * 100.0
    return max(-TONE_SCORE_CLIP, min(TONE_SCORE_CLIP, normalized))


def calculate_tone_delta(
    current_score: float,
    prior_score: float | None = None,
) -> float | None:
    """計算相較於前期的管理層態度邊際變化分數；無前期可比時回傳 None（不以當期分數冒充）。"""
    if prior_score is None:
        return None
    return current_score - prior_score


def calculate_extraction_confidence(
    extraction: GuidanceExtraction,
    grounded_tone_count: int,
) -> float:
    """依擷取欄位完整度計算可解釋之信心分數，範圍 [0.0, 1.0]。

    證據項共 7 項，每項等權：
    - 四維態度中，引文可在新聞稿原文逐字溯源者（每維 1 項，共 4 項）
    - 營收指引中點、EPS 指引中點、利潤率指引（各 1 項，共 3 項）
    """
    tone_items = max(0, min(4, grounded_tone_count))
    numeric_items = sum(
        (
            extraction.revenue_guidance_midpoint_usd is not None,
            extraction.eps_guidance_midpoint_usd is not None,
            bool(extraction.margin_guidance),
        )
    )
    return round((tone_items + numeric_items) / CONFIDENCE_EVIDENCE_ITEMS, 4)


def evaluate_margin_trend(extraction: GuidanceExtraction) -> str:
    """分析利潤率指引之整體方向趨勢。"""
    if not extraction.margin_guidance:
        return "無明示指引"

    expanding = 0
    compressing = 0
    flat = 0

    for m in extraction.margin_guidance:
        if m.direction == "EXPANDING":
            expanding += 1
        elif m.direction == "COMPRESSING":
            compressing += 1
        elif m.direction == "FLAT":
            flat += 1

    if expanding > compressing:
        return "利潤率擴張"
    elif compressing > expanding:
        return "利潤率承壓"
    elif expanding > 0 and expanding == compressing:
        return "利潤率分歧"
    elif flat > 0:
        return "利潤率持平"
    return "無明示指引"


VERDICT_LABELS_ZH: dict[str, str] = {
    "RAISED": "調升",
    "LOWERED": "調降",
    "MAINTAINED": "維持",
    "UNKNOWN": "無法判定",
}


def is_magnitude_comparable(
    current: float, reference: float, floor: float = 0.0
) -> bool:
    """檢查前後數值是否同一量綱（防範一筆以美元、一筆以百萬 / 十億美元填寫）。

    兩者絕對值（以 floor 為下限）差距超過 MAGNITUDE_MISMATCH_RATIO（100 倍）即視為單位不一致。
    """
    big = max(abs(current), abs(reference))
    small = max(min(abs(current), abs(reference)), floor)
    if small <= 0:
        return big <= 0
    return big / small <= MAGNITUDE_MISMATCH_RATIO


def compare_guidance(
    current: GuidanceExtraction,
    prior: GuidanceExtraction | None = None,
    consensus_rev: float | None = None,
    consensus_eps: float | None = None,
) -> GuidanceDeltaSummary:
    """比對當期指引、前期指引與市場共識，產出前瞻指引邊際裁決。

    規則（docs/valuation_pricing/05 §2.5）：
    - 數值比較只在「同一目標期別」之間進行：前期指引須與當期指引的
      `guidance_target_period` 正規化後相同；共識值由呼叫端負責對齊同一期別。
    - 前後數值量綱差距逾 100 倍（單位不一致）時不比較。
    - 無任何可比較之數值變化時一律回傳 UNKNOWN，不以語意態度推論調升 / 調降。
    """
    current_tone = calculate_management_tone_score(current)
    prior_tone = calculate_management_tone_score(prior) if prior is not None else None
    tone_delta = calculate_tone_delta(current_tone, prior_tone)

    notes: list[str] = []
    comparable_prior: GuidanceExtraction | None = None
    curr_target = normalize_guidance_period(current.guidance_target_period)
    if prior is not None:
        prior_target = normalize_guidance_period(prior.guidance_target_period)
        if curr_target is not None and curr_target == prior_target:
            comparable_prior = prior
        else:
            notes.append(
                f"前期指引目標期別不同或不明（{prior_target or '不明'} → {curr_target or '不明'}）"
            )

    # 1. 營收指引邊際變動
    rev_delta_pct: float | None = None
    curr_rev = current.revenue_guidance_midpoint_usd
    if curr_rev is not None and curr_rev > 0:
        prior_rev = (
            comparable_prior.revenue_guidance_midpoint_usd
            if comparable_prior is not None
            else None
        )
        if prior_rev is not None and prior_rev > 0:
            if is_magnitude_comparable(curr_rev, prior_rev):
                rev_delta_pct = (curr_rev - prior_rev) / prior_rev
            else:
                notes.append("營收指引前後單位不一致")
        elif consensus_rev is not None and consensus_rev > 0:
            if is_magnitude_comparable(curr_rev, consensus_rev):
                rev_delta_pct = (curr_rev - consensus_rev) / consensus_rev
            else:
                notes.append("營收指引與共識單位不一致")

    # 2. EPS 指引邊際變動
    eps_delta_pct: float | None = None
    curr_eps = current.eps_guidance_midpoint_usd
    if curr_eps is not None:
        prior_eps = (
            comparable_prior.eps_guidance_midpoint_usd
            if comparable_prior is not None
            else None
        )
        if prior_eps is not None:
            if is_magnitude_comparable(curr_eps, prior_eps, FLOOR_EPS):
                eps_delta_pct = (curr_eps - prior_eps) / max(abs(prior_eps), FLOOR_EPS)
            else:
                notes.append("EPS 指引前後單位不一致")
        elif consensus_eps is not None:
            if is_magnitude_comparable(curr_eps, consensus_eps, FLOOR_EPS):
                eps_delta_pct = (curr_eps - consensus_eps) / max(
                    abs(consensus_eps), FLOOR_EPS
                )
            else:
                notes.append("EPS 指引與共識單位不一致")

    margin_trend = evaluate_margin_trend(current)

    # 3. 裁決判定 (Verdict Arbitration)
    verdict: GuidanceVerdict = "UNKNOWN"
    has_numerical = rev_delta_pct is not None or eps_delta_pct is not None

    if has_numerical:
        r_up = rev_delta_pct is not None and rev_delta_pct >= RAISE_THRESHOLD_PCT
        e_up = eps_delta_pct is not None and eps_delta_pct >= RAISE_THRESHOLD_PCT
        r_down = rev_delta_pct is not None and rev_delta_pct <= LOWER_THRESHOLD_PCT
        e_down = eps_delta_pct is not None and eps_delta_pct <= LOWER_THRESHOLD_PCT

        if (r_up or e_up) and not (r_down or e_down):
            verdict = "RAISED"
        elif (r_down or e_down) and not (r_up or e_up):
            verdict = "LOWERED"
        elif (r_up or e_up) and (r_down or e_down):
            # 數值方向分歧 (例如營收調升但 EPS 調降): 依態度與利潤率仲裁
            if current_tone <= -15.0 or "承壓" in margin_trend:
                verdict = "LOWERED"
            elif current_tone >= 15.0 and "擴張" in margin_trend:
                verdict = "RAISED"
            else:
                verdict = "MAINTAINED"
        else:
            verdict = "MAINTAINED"
    elif not notes:
        notes.append("無同期別前後數值指引")

    # 4. 生成 100% 繁體中文摘要
    summary_parts: list[str] = [f"指引裁決: **{VERDICT_LABELS_ZH[verdict]}**"]
    if rev_delta_pct is not None:
        summary_parts.append(f"營收指引變化: `{rev_delta_pct:+.1%}`")
    if eps_delta_pct is not None:
        summary_parts.append(f"EPS 指引變化: `{eps_delta_pct:+.1%}`")
    summary_parts.append(f"管理層語意態度: `{current_tone:+.1f}` ({margin_trend})")

    return GuidanceDeltaSummary(
        tone_score=round(current_tone, 2),
        tone_delta=round(tone_delta, 2) if tone_delta is not None else None,
        revenue_guidance_delta_pct=round(rev_delta_pct, 4)
        if rev_delta_pct is not None
        else None,
        eps_guidance_delta_pct=round(eps_delta_pct, 4)
        if eps_delta_pct is not None
        else None,
        margin_trend=margin_trend,
        verdict=verdict,
        summary_text=" ｜ ".join(summary_parts),
        comparison_note="；".join(notes),
    )
