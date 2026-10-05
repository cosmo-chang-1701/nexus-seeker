"""財務預期差綜合評分量化模型 (Earnings Surprise Metric)。

職責：
1. 計算每股盈餘 (EPS) 雙維度預期差 (包含 GAAP/Non-GAAP 與 Whisper 預期)。
2. 計算營業收入 (Revenue) 預期差。
3. 處理除以接近零奇點 (FLOOR_EPS 物理門檻與 small_base 標記)。
4. 計算綜合驚喜分數 (Composite Surprise Score)，並支援單項缺失權重自動重新正規化。
"""

from __future__ import annotations

import logging

from market_analysis.fundamental_pipeline.models import EarningsSurpriseResult

logger = logging.getLogger(__name__)

# ============================================================================
# 具名常數與物理邊界 (Named Constants & Physical Boundaries)
# ============================================================================

FLOOR_EPS: float = 0.05  # EPS 共識值基準保護門檻 (美元)
EPS_SURPRISE_CLIP: float = 0.50  # EPS 預期差雙向箝制 (±50%)
REV_SURPRISE_CLIP: float = 0.10  # 營收預期差雙向箝制 (±10%)
WEIGHT_EPS: float = 0.60  # 綜合評分中 EPS 之基準權重
WEIGHT_REV: float = 0.40  # 綜合評分中 Revenue 之基準權重
SCORE_CLIP: float = 100.0  # 綜合驚喜分數雙向箝制 (±100.0)


def calculate_eps_surprise(
    actual: float | None,
    consensus: float | None,
    floor_eps: float = FLOOR_EPS,
    clip_limit: float = EPS_SURPRISE_CLIP,
) -> tuple[float | None, bool]:
    """計算每股盈餘 (EPS) 預期差百分比。

    公式：
        Surprise_EPS = (Actual - Consensus) / max(|Consensus|, FLOOR_EPS)

    回傳：
        (surprise_pct, small_base)
        若任一數值為 None 則回傳 (None, False)。
        若 |Consensus| < floor_eps 則標註 small_base = True。
    """
    if actual is None or consensus is None:
        return None, False

    abs_consensus = abs(consensus)
    small_base = abs_consensus < floor_eps
    denom = max(abs_consensus, floor_eps)

    raw_surprise = (actual - consensus) / denom
    clamped_surprise = max(-clip_limit, min(clip_limit, raw_surprise))
    return clamped_surprise, small_base


def calculate_revenue_surprise(
    actual: float | None,
    consensus: float | None,
    clip_limit: float = REV_SURPRISE_CLIP,
) -> float | None:
    """計算營業收入 (Revenue) 預期差百分比。

    公式：
        Surprise_REV = (Actual - Consensus) / |Consensus|

    回傳：
        surprise_pct，箝制於 [-clip_limit, +clip_limit]；
        若任一數值缺失或 consensus <= 0 則回傳 None。
    """
    if actual is None or consensus is None or consensus <= 0:
        return None

    raw_surprise = (actual - consensus) / abs(consensus)
    return max(-clip_limit, min(clip_limit, raw_surprise))


def calculate_whisper_surprise(
    actual: float | None,
    whisper: float | None,
    floor_eps: float = FLOOR_EPS,
    clip_limit: float = EPS_SURPRISE_CLIP,
) -> tuple[float | None, bool]:
    """計算耳語每股盈餘 (Whisper EPS) 預期差百分比。

    公式：
        Surprise_Whisper = (Actual - Whisper) / max(|Whisper|, FLOOR_EPS)
    """
    return calculate_eps_surprise(
        actual=actual,
        consensus=whisper,
        floor_eps=floor_eps,
        clip_limit=clip_limit,
    )


def calculate_composite_surprise_score(
    eps_surprise: float | None,
    rev_surprise: float | None,
    whisper_surprise: float | None = None,
    whisper_weight: float = 0.0,
    weight_eps: float = WEIGHT_EPS,
    weight_rev: float = WEIGHT_REV,
    score_clip: float = SCORE_CLIP,
) -> float | None:
    """計算綜合驚喜分數 (Composite Surprise Score)。

    雙向箝制於 [-100.0, +100.0]。
    若單項數據缺失，自動將現存單項之權重重新正規化為 1.0；若全數缺失則回傳 None。
    若提供 whisper_surprise 且 whisper_weight > 0，則按比例由 EPS 權重分配予 Whisper。
    """
    # 決定 EPS 總項與 Whisper 分配
    has_eps = eps_surprise is not None
    has_rev = rev_surprise is not None
    has_whisper = whisper_surprise is not None and whisper_weight > 0

    if not has_eps and not has_rev and not has_whisper:
        return None

    # 有效項目及其未正規化貢獻
    components: list[
        tuple[float, float, float]
    ] = []  # (normalized_val, base_weight, scale)

    if has_eps and eps_surprise is not None:
        effective_eps_weight = (
            weight_eps * (1.0 - whisper_weight) if has_whisper else weight_eps
        )
        components.append((eps_surprise, effective_eps_weight, EPS_SURPRISE_CLIP))

    if has_whisper and whisper_surprise is not None:
        components.append(
            (whisper_surprise, weight_eps * whisper_weight, EPS_SURPRISE_CLIP)
        )

    if has_rev and rev_surprise is not None:
        components.append((rev_surprise, weight_rev, REV_SURPRISE_CLIP))

    total_weight = sum(w for _, w, _ in components)
    if total_weight <= 0:
        return None

    # 加權平均計算
    weighted_sum = sum(
        (val / scale) * (w / total_weight) for val, w, scale in components
    )
    score = weighted_sum * 100.0
    return max(-score_clip, min(score_clip, score))


def evaluate_earnings_surprise(
    actual_eps: float | None,
    consensus_eps: float | None,
    actual_revenue: float | None,
    consensus_revenue: float | None,
    whisper_eps: float | None = None,
    whisper_weight: float = 0.0,
    floor_eps: float = FLOOR_EPS,
) -> EarningsSurpriseResult:
    """執行完整財務預期差綜合評估。"""
    eps_surprise, small_base = calculate_eps_surprise(
        actual=actual_eps,
        consensus=consensus_eps,
        floor_eps=floor_eps,
    )
    rev_surprise = calculate_revenue_surprise(
        actual=actual_revenue,
        consensus=consensus_revenue,
    )
    whisper_surprise: float | None = None
    if whisper_eps is not None and actual_eps is not None:
        whisper_surprise, _ = calculate_whisper_surprise(
            actual=actual_eps,
            whisper=whisper_eps,
            floor_eps=floor_eps,
        )

    composite_score = calculate_composite_surprise_score(
        eps_surprise=eps_surprise,
        rev_surprise=rev_surprise,
        whisper_surprise=whisper_surprise,
        whisper_weight=whisper_weight,
    )

    return EarningsSurpriseResult(
        eps_surprise_pct=eps_surprise,
        revenue_surprise_pct=rev_surprise,
        whisper_surprise_pct=whisper_surprise,
        composite_score=composite_score,
        small_base=small_base,
    )
