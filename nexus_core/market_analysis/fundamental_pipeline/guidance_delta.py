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
) -> float:
    """計算相較於前期的管理層態度邊際變化分數。"""
    if prior_score is None:
        return current_score
    return current_score - prior_score


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
        return "利潤率擴張 (Expanding)"
    elif compressing > expanding:
        return "利潤率承壓 (Compressing)"
    elif expanding > 0 and expanding == compressing:
        return "利潤率分歧 (Mixed)"
    elif flat > 0:
        return "利潤率持平 (Flat)"
    return "無明示指引"


def compare_guidance(
    current: GuidanceExtraction,
    prior: GuidanceExtraction | None = None,
    consensus_rev: float | None = None,
    consensus_eps: float | None = None,
) -> GuidanceDeltaSummary:
    """綜合比對當期指引、前期指引與市場共識，產出前瞻指引邊際裁決。"""
    current_tone = calculate_management_tone_score(current)
    prior_tone = calculate_management_tone_score(prior) if prior is not None else None
    tone_delta = calculate_tone_delta(current_tone, prior_tone)

    # 1. 營收指引邊際變動
    rev_delta_pct: float | None = None
    curr_rev = current.revenue_guidance_midpoint_usd
    if curr_rev is not None and curr_rev > 0:
        if (
            prior is not None
            and prior.revenue_guidance_midpoint_usd
            and prior.revenue_guidance_midpoint_usd > 0
        ):
            rev_delta_pct = (
                curr_rev - prior.revenue_guidance_midpoint_usd
            ) / prior.revenue_guidance_midpoint_usd
        elif consensus_rev is not None and consensus_rev > 0:
            rev_delta_pct = (curr_rev - consensus_rev) / consensus_rev

    # 2. EPS 指引邊際變動
    eps_delta_pct: float | None = None
    curr_eps = current.eps_guidance_midpoint_usd
    if curr_eps is not None:
        if prior is not None and prior.eps_guidance_midpoint_usd is not None:
            prior_eps = prior.eps_guidance_midpoint_usd
            denom = max(abs(prior_eps), FLOOR_EPS)
            eps_delta_pct = (curr_eps - prior_eps) / denom
        elif consensus_eps is not None:
            denom = max(abs(consensus_eps), FLOOR_EPS)
            eps_delta_pct = (curr_eps - consensus_eps) / denom

    margin_trend = evaluate_margin_trend(current)

    # 3. 裁決判定 (Verdict Arbitration)
    verdict: GuidanceVerdict = "UNKNOWN"
    has_numerical = rev_delta_pct is not None or eps_delta_pct is not None

    if has_numerical:
        # 有具體數值指引時以數值為主
        r_up = rev_delta_pct is not None and rev_delta_pct >= RAISE_THRESHOLD_PCT
        e_up = eps_delta_pct is not None and eps_delta_pct >= RAISE_THRESHOLD_PCT
        r_down = rev_delta_pct is not None and rev_delta_pct <= LOWER_THRESHOLD_PCT
        e_down = eps_delta_pct is not None and eps_delta_pct <= LOWER_THRESHOLD_PCT

        if (r_up or e_up) and not (r_down or e_down):
            verdict = "RAISED"
        elif (r_down or e_down) and not (r_up or e_up):
            verdict = "LOWERED"
        elif (r_up or e_up) and (r_down or e_down):
            # 數值方向分歧 (例如營收調升但利潤/EPS調降): 依態度與利潤率仲裁
            if current_tone <= -15.0 or "承壓" in margin_trend:
                verdict = "LOWERED"
            elif current_tone >= 15.0 and "擴張" in margin_trend:
                verdict = "RAISED"
            else:
                verdict = "MAINTAINED"
        else:
            verdict = "MAINTAINED"
    else:
        # 缺乏數值指引時退回語意與利潤率綜合評估
        if current_tone >= 25.0 and "承壓" not in margin_trend:
            verdict = "RAISED"
        elif current_tone <= -25.0 or "承壓" in margin_trend:
            verdict = "LOWERED"
        elif current.reasoning_traditional_chinese:
            verdict = "MAINTAINED"
        else:
            verdict = "UNKNOWN"

    # 4. 生成 100% 繁體中文摘要
    summary_parts: list[str] = [f"指引裁決: **{verdict}**"]
    if rev_delta_pct is not None:
        summary_parts.append(f"營收指引變化: `{rev_delta_pct:+.1%}`")
    if eps_delta_pct is not None:
        summary_parts.append(f"EPS 指引變化: `{eps_delta_pct:+.1%}`")
    summary_parts.append(f"管理層語意態度: `{current_tone:+.1f}` ({margin_trend})")

    return GuidanceDeltaSummary(
        tone_score=round(current_tone, 2),
        tone_delta=round(tone_delta, 2),
        revenue_guidance_delta_pct=round(rev_delta_pct, 4)
        if rev_delta_pct is not None
        else None,
        eps_guidance_delta_pct=round(eps_delta_pct, 4)
        if eps_delta_pct is not None
        else None,
        margin_trend=margin_trend,
        verdict=verdict,
        summary_text=" ｜ ".join(summary_parts),
    )
