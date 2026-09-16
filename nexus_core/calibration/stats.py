"""統計工具：Wilson 區間、依日期叢集的 bootstrap、收縮估計、充足性判定。"""

import math
from dataclasses import dataclass
from typing import Optional, Sequence

import numpy as np
import pandas as pd

_SPLIT_MIN_TRAIN_SHARE = 0.40
_SPLIT_MAX_TRAIN_SHARE = 0.85


def wilson_interval(wins: int, n: int, z: float = 1.959964) -> tuple[float, float]:
    if n <= 0:
        return (0.0, 1.0)
    p = wins / n
    denom = 1.0 + z * z / n
    center = (p + z * z / (2 * n)) / denom
    half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / denom
    return (max(0.0, center - half), min(1.0, center + half))


def clustered_bootstrap_mean(
    values: Sequence[float],
    clusters: Sequence[object],
    n_boot: int,
    seed: int,
) -> tuple[float, float, float]:
    """(平均, 2.5%, 97.5%)。以叢集 (事件日期) 為單位重抽：同一天橫斷面上的事件
    高度相關，逐筆重抽會嚴重低估區間寬度。"""
    vals = np.asarray(values, dtype="float64")
    if vals.size == 0:
        return (float("nan"), float("nan"), float("nan"))
    labels = pd.Series(list(clusters)).astype(str).to_numpy()
    uniq, inverse = np.unique(labels, return_inverse=True)
    sums = np.bincount(inverse, weights=vals, minlength=len(uniq))
    counts = np.bincount(inverse, minlength=len(uniq)).astype("float64")
    mean = float(vals.mean())
    if len(uniq) < 2:
        return (mean, mean, mean)
    rng = np.random.default_rng(seed)
    idx = rng.integers(0, len(uniq), size=(n_boot, len(uniq)))
    boot = sums[idx].sum(axis=1) / counts[idx].sum(axis=1)
    lo, hi = np.percentile(boot, [2.5, 97.5])
    return (mean, float(lo), float(hi))


def effective_oos_split(dates: pd.Series, default_split: str) -> str:
    """樣本外切點。

    切點前的樣本占比落在 [40%, 85%] 時沿用固定切點 (日線多年資料的常態)；否則
    改用事件日期的 70% 分位數。1h 資料只有約兩年，固定切點可能只切出幾週的訓練
    期 (或整段都在切點之後)，那樣的「樣本外一致」沒有意義。
    """
    if dates.empty:
        return default_split
    ordered = dates.astype(str).sort_values().reset_index(drop=True)
    before_share = float((ordered < default_split).mean())
    if _SPLIT_MIN_TRAIN_SHARE <= before_share <= _SPLIT_MAX_TRAIN_SHARE:
        return default_split
    return str(ordered.iloc[max(0, int(len(ordered) * 0.7) - 1)])


def shrink(current: float, estimate: float, n: int, n0: int) -> float:
    """向現行值收縮：n 越少越貼近現行值。"""
    if n <= 0 or not math.isfinite(estimate):
        return current
    w = n / (n + n0)
    return current + w * (estimate - current)


@dataclass(frozen=True)
class BucketStats:
    n: int
    n_dates: int
    win_rate: float
    wilson_lo: float
    wilson_hi: float
    expectancy: float
    exp_lo: float
    exp_hi: float
    oos_n: int
    oos_expectancy: Optional[float]
    oos_agrees: bool


def summarize(
    labeled: pd.DataFrame,
    n_boot: int,
    seed: int,
    oos_split: str,
    win_col: str = "win",
    r_col: str = "r_multiple",
) -> BucketStats:
    n = int(len(labeled))
    if n == 0:
        return BucketStats(
            0,
            0,
            float("nan"),
            0.0,
            1.0,
            float("nan"),
            float("nan"),
            float("nan"),
            0,
            None,
            False,
        )
    wins = int(labeled[win_col].sum())
    lo, hi = wilson_interval(wins, n)
    exp, exp_lo, exp_hi = clustered_bootstrap_mean(
        labeled[r_col].to_numpy(), labeled["date"].to_numpy(), n_boot, seed
    )
    split = effective_oos_split(labeled["date"], oos_split)
    in_sample = labeled[labeled["date"] < split]
    oos = labeled[labeled["date"] >= split]
    oos_exp = float(oos[r_col].mean()) if len(oos) else None
    ins_exp = float(in_sample[r_col].mean()) if len(in_sample) else None
    oos_agrees = (
        oos_exp is not None
        and ins_exp is not None
        and np.sign(oos_exp) == np.sign(ins_exp)
        and oos_exp != 0.0
    )
    return BucketStats(
        n=n,
        n_dates=int(labeled["date"].nunique()),
        win_rate=wins / n,
        wilson_lo=lo,
        wilson_hi=hi,
        expectancy=exp,
        exp_lo=exp_lo,
        exp_hi=exp_hi,
        oos_n=int(len(oos)),
        oos_expectancy=oos_exp,
        oos_agrees=bool(oos_agrees),
    )


def is_sufficient(stats: BucketStats, min_events: int, min_dates: int) -> bool:
    return stats.n >= min_events and stats.n_dates >= min_dates and stats.oos_agrees
