"""分析師修正動能模型與 PEAD 捕捉 (Revision Momentum & PEAD)。

本模組為純演算法葉模組，無外部 I/O 副作用。
依據分析師 30 天前後各期每股盈餘預估中值之變動斜率與調升/調降廣度比率，
評定標的基本面動能分數，並在財報發布 60 天內驗證盈餘公布後漂移 (PEAD) 共振標記。

數學規範：
- 斜率計算：
  Slope_h = (EPS_{h, t} - EPS_{h, t-30d}) / max(|EPS_{h, t-30d}|, FLOOR_EPS)
  h in {"0q", "+1q", "0y", "+1y"}
  FLOOR_EPS = 0.05
- 權重配置：
  w_{0q} = 0.20, w_{+1q} = 0.30, w_{0y} = 0.20, w_{+1y} = 0.30
- 廣度比率：
  Breadth = (N_up - N_down) / (N_up + N_down + eps)
- 基本面修正動能評分：
  Score_Rev = clip(100 * [0.70 * sum(w_h * clip(Slope_h / 0.20, -1, 1)) + 0.30 * Breadth], -100.0, 100.0)
- PEAD 共振：
  days <= 60 且 sign(Surprise) == sign(Score_Rev) 且 |Surprise| >= 15.0 且 |Score_Rev| >= 15.0
"""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from typing import Any

from market_analysis.fundamental_pipeline.models import (
    EPSEstimateSnapshotRecord,
    RevisionMomentumResult,
)

FLOOR_EPS: float = 0.05
SLOPE_NORMALIZER: float = 0.20
SLOPE_WEIGHT: float = 0.70
BREADTH_WEIGHT: float = 0.30
PEAD_MAX_DAYS: int = 60
PEAD_THRESHOLD: float = 15.0

HORIZON_WEIGHTS: dict[str, float] = {
    "0q": 0.20,
    "+1q": 0.30,
    "0y": 0.20,
    "+1y": 0.30,
}


def calculate_horizon_slope(
    current_eps: float, prior_eps: float, floor_eps: float = FLOOR_EPS
) -> float:
    """計算單一期限之分析師預估變動斜率。

    分母取 max(|prior_eps|, floor_eps)，避免微小分母造成極端數值膨脹。
    """
    denom = max(abs(prior_eps), floor_eps)
    return (current_eps - prior_eps) / denom


def calculate_breadth(up_count: int, down_count: int, epsilon: float = 1e-6) -> float:
    """計算分析師調升/調降廣度比率 (Breadth Ratio)。

    若無任何調整 (up=0, down=0)，回傳 0.0。
    數值範圍 [-1.0, 1.0]。
    """
    total = up_count + down_count
    if total <= 0:
        return 0.0
    raw_breadth = (up_count - down_count) / float(total)
    return max(-1.0, min(1.0, raw_breadth))


def calculate_revision_score(slopes: Mapping[str, float], breadth: float) -> float:
    """計算基本面分析師修正動能綜合評分。

    - 對於可用之期限斜率進行權重重新正規化。
    - 各期限斜率標準化至 [-1.0, 1.0] (以 0.20 為滿分尺度)。
    - 綜合分數箝制於 [-100.0, +100.0]。
    """
    valid_slopes: dict[str, float] = {}
    for h, s in slopes.items():
        if h in HORIZON_WEIGHTS and math.isfinite(s):
            valid_slopes[h] = s

    if valid_slopes:
        total_weight = sum(HORIZON_WEIGHTS[h] for h in valid_slopes)
        if total_weight > 0:
            weighted_slope_norm = sum(
                (HORIZON_WEIGHTS[h] / total_weight)
                * max(-1.0, min(1.0, valid_slopes[h] / SLOPE_NORMALIZER))
                for h in valid_slopes
            )
        else:
            weighted_slope_norm = 0.0
    else:
        weighted_slope_norm = 0.0

    raw_score = 100.0 * (SLOPE_WEIGHT * weighted_slope_norm + BREADTH_WEIGHT * breadth)
    return max(-100.0, min(100.0, raw_score))


def is_pead_aligned(
    surprise_score: float | None,
    revision_score: float | None,
    days_since_surprise: int | None,
) -> bool:
    """判定標的是否符合盈餘公布後漂移 (PEAD) 共振條件。

    條件：
    1. 距離財報發布 <= 60 交易日。
    2. 財務預期差綜合評分與分析師修正動能同號 (皆正或皆負)。
    3. 兩者絕對值皆 >= 15.0 (具備顯著性)。
    """
    if surprise_score is None or revision_score is None or days_since_surprise is None:
        return False

    if not math.isfinite(surprise_score) or not math.isfinite(revision_score):
        return False

    if days_since_surprise < 0 or days_since_surprise > PEAD_MAX_DAYS:
        return False

    if abs(surprise_score) < PEAD_THRESHOLD or abs(revision_score) < PEAD_THRESHOLD:
        return False

    return (surprise_score > 0 and revision_score > 0) or (
        surprise_score < 0 and revision_score < 0
    )


def evaluate_revision_momentum(
    current_snapshots: Sequence[EPSEstimateSnapshotRecord],
    prior_snapshots: Sequence[EPSEstimateSnapshotRecord],
    surprise_score: float | None = None,
    days_since_surprise: int | None = None,
    analyst_up_count: int | None = None,
    analyst_down_count: int | None = None,
) -> RevisionMomentumResult:
    """綜合評估分析師修正動能與 PEAD 共振。

    比對 current_snapshots 與 prior_snapshots，依 horizon 配對計算各期斜率。
    若未直接提供分析師個別調幅計數 (analyst_up/down_count)，
    則以各期斜率方向作為廣度比率依據。
    """
    prior_by_horizon: dict[str, float] = {}
    for s in prior_snapshots:
        if (
            s.horizon
            and s.horizon not in prior_by_horizon
            and math.isfinite(s.eps_mean)
        ):
            prior_by_horizon[s.horizon] = s.eps_mean

    curr_by_horizon: dict[str, float] = {}
    for s in current_snapshots:
        if s.horizon and s.horizon not in curr_by_horizon and math.isfinite(s.eps_mean):
            curr_by_horizon[s.horizon] = s.eps_mean

    slopes: dict[str, float] = {}
    detected_up = 0
    detected_down = 0

    for h, curr_eps in curr_by_horizon.items():
        if h in prior_by_horizon:
            p_val = prior_by_horizon[h]
            slope_val = calculate_horizon_slope(curr_eps, p_val)
            slopes[h] = slope_val
            if slope_val > 0.001:
                detected_up += 1
            elif slope_val < -0.001:
                detected_down += 1

    final_up = analyst_up_count if analyst_up_count is not None else detected_up
    final_down = analyst_down_count if analyst_down_count is not None else detected_down

    breadth = calculate_breadth(final_up, final_down)
    score_30d = calculate_revision_score(slopes, breadth)
    pead_flag = is_pead_aligned(surprise_score, score_30d, days_since_surprise)

    details: dict[str, Any] = {
        "slopes": {k: round(v, 4) for k, v in slopes.items()},
        "breadth": round(breadth, 4),
        "up_count": final_up,
        "down_count": final_down,
        "days_since_surprise": days_since_surprise,
        "surprise_score": surprise_score,
    }

    return RevisionMomentumResult(
        score_30d=round(score_30d, 2),
        breadth_ratio=round(breadth, 4),
        is_pead_aligned=pead_flag,
        slopes=slopes,
        up_count=final_up,
        down_count=final_down,
        details=details,
    )
