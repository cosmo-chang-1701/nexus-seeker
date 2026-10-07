"""分析師修正動能模型與 PEAD 捕捉 (Revision Momentum & PEAD)。

本模組為純演算法葉模組，無外部 I/O 副作用。
依據分析師 t 與 t-30d 各期每股盈餘預估中值之變動斜率（以**財期**配對）與調升/調降廣度比率，
評定標的基本面動能分數，並在財報發布 60 個交易日內驗證盈餘公布後漂移 (PEAD) 共振標記。

數學規範（規格：docs/valuation_pricing/06_revision_momentum_and_fair_value.md §2.5）：
- 斜率計算（同一財期 F 之 t 與 t-30d 預估；t 時的 horizon 標籤 h 決定權重）：
  Slope_h = (EPS_{F, t} - EPS_{F, t-30d}) / max(|EPS_{F, t-30d}|, FLOOR_EPS)
  t-30d 取 [t-35, t-28] 窗口內最接近 t-30 的快照日；同財期找不到基準者該 horizon 不計。
- 權重配置：
  w_{0q} = 0.20, w_{+1q} = 0.30, w_{0y} = 0.20, w_{+1y} = 0.30（缺失項重新正規化）
  Finnhub 免費方案只有 0q / +1q（eps-estimate 回 403，走財報日曆備援）。
- 廣度比率（需分析師層級調升 / 調降計數）：
  Breadth = (N_up - N_down) / (N_up + N_down)
  無分析師層級計數時 Breadth 不計（None），斜率項權重重新正規化為 1.0；
  不以各期斜率方向充當廣度（會與斜率項重複計分）。
- 基本面修正動能評分：
  有廣度：Score_Rev = clip(100 * [0.70 * S + 0.30 * Breadth], -100, 100)
  無廣度：Score_Rev = clip(100 * S, -100, 100)
  S = sum(w_h * clip(Slope_h / 0.20, -1, 1))；無任何可配對財期時 Score_Rev = None。
- PEAD 共振：
  交易日數 <= 60 且 sign(Surprise) == sign(Score_Rev) 且 |Surprise| >= 15.0 且 |Score_Rev| >= 15.0
"""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from datetime import date, timedelta
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

# t-30d 基準快照窗口：目標 t-30，僅接受 [t-35, t-28] 內的快照（窗口外不退回）
PRIOR_TARGET_DAYS: int = 30
PRIOR_WINDOW_MIN_DAYS: int = 28
PRIOR_WINDOW_MAX_DAYS: int = 35

HORIZON_WEIGHTS: dict[str, float] = {
    "0q": 0.20,
    "+1q": 0.30,
    "0y": 0.20,
    "+1y": 0.30,
}

# detail_json 內的降級旗標
FLAG_NO_CURRENT_SNAPSHOT = "NO_CURRENT_SNAPSHOT"
FLAG_BREADTH_UNAVAILABLE = "BREADTH_UNAVAILABLE"
FLAG_NO_PRIOR_SNAPSHOT = "NO_PRIOR_SNAPSHOT"
FLAG_NO_MATCHED_PERIOD = "NO_MATCHED_PERIOD"
FLAG_PEAD_DATE_UNKNOWN = "PEAD_DATE_UNKNOWN"


def prior_window(as_of: date) -> tuple[date, date, date]:
    """回傳 (窗口起日 t-35, 窗口迄日 t-28, 目標日 t-30)。"""
    return (
        as_of - timedelta(days=PRIOR_WINDOW_MAX_DAYS),
        as_of - timedelta(days=PRIOR_WINDOW_MIN_DAYS),
        as_of - timedelta(days=PRIOR_TARGET_DAYS),
    )


def calculate_horizon_slope(
    current_eps: float, prior_eps: float, floor_eps: float = FLOOR_EPS
) -> float:
    """計算單一財期之分析師預估變動斜率。

    分母取 max(|prior_eps|, floor_eps)，避免微小分母造成極端數值膨脹。
    """
    denom = max(abs(prior_eps), floor_eps)
    return (current_eps - prior_eps) / denom


def calculate_breadth(up_count: int, down_count: int) -> float:
    """計算分析師調升/調降廣度比率 (Breadth Ratio)。

    若無任何調整 (up=0, down=0)，回傳 0.0。數值範圍 [-1.0, 1.0]。
    """
    total = up_count + down_count
    if total <= 0:
        return 0.0
    raw_breadth = (up_count - down_count) / float(total)
    return max(-1.0, min(1.0, raw_breadth))


def calculate_revision_score(
    slopes: Mapping[str, float], breadth: float | None
) -> float | None:
    """計算基本面分析師修正動能綜合評分。

    - 對於可用之 horizon 斜率進行權重重新正規化，各斜率標準化至 [-1.0, 1.0]（0.20 為滿分尺度）。
    - breadth 為 None（無分析師層級計數）時斜率項權重為 1.0。
    - 無任何有效斜率時回傳 None；分數箝制於 [-100.0, +100.0]。
    """
    valid_slopes = {
        h: s for h, s in slopes.items() if h in HORIZON_WEIGHTS and math.isfinite(s)
    }
    if not valid_slopes:
        return None
    total_weight = sum(HORIZON_WEIGHTS[h] for h in valid_slopes)
    weighted_slope_norm = sum(
        (HORIZON_WEIGHTS[h] / total_weight)
        * max(-1.0, min(1.0, valid_slopes[h] / SLOPE_NORMALIZER))
        for h in valid_slopes
    )
    if breadth is None:
        raw_score = 100.0 * weighted_slope_norm
    else:
        raw_score = 100.0 * (
            SLOPE_WEIGHT * weighted_slope_norm + BREADTH_WEIGHT * breadth
        )
    return max(-100.0, min(100.0, raw_score))


def is_pead_aligned(
    surprise_score: float | None,
    revision_score: float | None,
    days_since_surprise: int | None,
) -> bool:
    """判定標的是否符合盈餘公布後漂移 (PEAD) 共振條件。

    條件：
    1. 距離財報發布 <= 60 個 NYSE 交易日（呼叫端以交易日計算）。
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


def _days_from_target(snapshot_date: str, target: date | None) -> int:
    if target is None:
        return 0
    try:
        return abs((date.fromisoformat(snapshot_date[:10]) - target).days)
    except ValueError:
        return 10**6


def evaluate_revision_momentum(
    current_snapshots: Sequence[EPSEstimateSnapshotRecord],
    prior_snapshots: Sequence[EPSEstimateSnapshotRecord],
    surprise_score: float | None = None,
    days_since_surprise: int | None = None,
    analyst_up_count: int | None = None,
    analyst_down_count: int | None = None,
    prior_target_date: date | None = None,
) -> RevisionMomentumResult:
    """綜合評估分析師修正動能與 PEAD 共振。

    - 以財期 (fiscal_period) 配對：t 時的 horizon h（財期 F）對上 t-30d 窗口內同一財期 F 的預估，
      prior 的 horizon 標籤可能不同（跨季後 t-30d 的 +1q 會變成 t 的 0q）。
      t-30d 同財期有多個快照日時取最接近 prior_target_date 者。
    - 缺財期（v094 前寫入的快照）或同財期找不到基準者，該 horizon 不計。
    - 廣度只採用呼叫端提供的分析師層級計數；未提供時為 None（降級，見模組說明）。
    """
    flags: list[str] = []

    prior_by_period: dict[str, EPSEstimateSnapshotRecord] = {}
    for s in prior_snapshots:
        if not s.fiscal_period or not math.isfinite(s.eps_mean):
            continue
        existing = prior_by_period.get(s.fiscal_period)
        if existing is None or _days_from_target(
            s.snapshot_date, prior_target_date
        ) < _days_from_target(existing.snapshot_date, prior_target_date):
            prior_by_period[s.fiscal_period] = s

    curr_by_horizon: dict[str, EPSEstimateSnapshotRecord] = {}
    for s in current_snapshots:
        if s.horizon and s.horizon not in curr_by_horizon and math.isfinite(s.eps_mean):
            curr_by_horizon[s.horizon] = s

    slopes: dict[str, float] = {}
    pairs: dict[str, dict[str, Any]] = {}
    unmatched: list[str] = []
    detected_up = 0
    detected_down = 0

    for h, curr in curr_by_horizon.items():
        prior = prior_by_period.get(curr.fiscal_period) if curr.fiscal_period else None
        if prior is None:
            unmatched.append(h)
            continue
        slope_val = calculate_horizon_slope(curr.eps_mean, prior.eps_mean)
        slopes[h] = slope_val
        pairs[h] = {
            "fiscal_period": curr.fiscal_period,
            "current_eps": curr.eps_mean,
            "prior_eps": prior.eps_mean,
            "prior_date": prior.snapshot_date,
            "prior_horizon": prior.horizon,
        }
        if slope_val > 0.001:
            detected_up += 1
        elif slope_val < -0.001:
            detected_down += 1

    if not curr_by_horizon:
        flags.append(FLAG_NO_CURRENT_SNAPSHOT)
    elif not prior_snapshots:
        flags.append(FLAG_NO_PRIOR_SNAPSHOT)
    elif not slopes:
        flags.append(FLAG_NO_MATCHED_PERIOD)

    breadth: float | None
    if analyst_up_count is not None and analyst_down_count is not None:
        breadth = calculate_breadth(analyst_up_count, analyst_down_count)
        up_count, down_count = analyst_up_count, analyst_down_count
    else:
        breadth = None
        flags.append(FLAG_BREADTH_UNAVAILABLE)
        # 僅供顯示：各財期斜率方向計數，不參與評分
        up_count, down_count = detected_up, detected_down

    score_30d = calculate_revision_score(slopes, breadth)
    if surprise_score is not None and days_since_surprise is None:
        flags.append(FLAG_PEAD_DATE_UNKNOWN)
    pead_flag = is_pead_aligned(surprise_score, score_30d, days_since_surprise)

    details: dict[str, Any] = {
        "slopes": {k: round(v, 4) for k, v in slopes.items()},
        "pairs": pairs,
        "unmatched_horizons": unmatched,
        "breadth": round(breadth, 4) if breadth is not None else None,
        "up_count": up_count,
        "down_count": down_count,
        "days_since_surprise": days_since_surprise,
        "surprise_score": surprise_score,
        "flags": flags,
    }

    return RevisionMomentumResult(
        score_30d=round(score_30d, 2) if score_30d is not None else None,
        breadth_ratio=round(breadth, 4) if breadth is not None else None,
        is_pead_aligned=pead_flag,
        slopes=slopes,
        up_count=up_count,
        down_count=down_count,
        details=details,
    )
