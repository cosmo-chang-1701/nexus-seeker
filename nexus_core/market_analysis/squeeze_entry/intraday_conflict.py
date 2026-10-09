"""擠壓進場的日內交叉檢核（僅呈現與前向紀錄，不影響 `evaluate_squeeze_entry` 判定）。

日線／週線的 T3 前提落後於日內走勢：D 已 Green Dot 時 65m／15m 可能已加速向下，
或最新 15m 已放量陰線跌穿 LVN。本模組把這兩類衝突整理成可讀字串，供 /x 的
Target Lock 與擠壓欄位（同源）以及 `evaluation_recorder` 的前向紀錄共用。

規格：`docs/strategies/10_multi_timeframe_squeeze_entry.md`。
"""

from __future__ import annotations

import math
from typing import Any, List, Mapping, Optional

from market_analysis.dynamic_rollover.constants import _ENTRY_VOLUME_SURGE_MULTIPLIER

# PRE_CALIBRATION／僅呈現：放量陰線的量比門檻，直接引用 /x 的 RVOL 放量定義，避免漂移。
INTRADAY_BREAK_RVOL: float = _ENTRY_VOLUME_SURGE_MULTIPLIER

_INTRADAY_TFS: tuple[str, ...] = ("65m", "15m")
_ACCELERATING_COLOR = "Red"  # 負且增強（Golden 為負但減弱）


def _momentum_or_none(state: Any) -> Optional[float]:
    try:
        mv = float(getattr(state, "momentum_value"))
    except (AttributeError, TypeError, ValueError):
        return None
    return mv if math.isfinite(mv) else None


def momentum_value_2dp(state: Any) -> Optional[float]:
    """單一框架的 momentum_value 取兩位小數；缺失或非有限值為 None（前向紀錄用）。"""
    mv = _momentum_or_none(state)
    return round(mv, 2) if mv is not None else None


def negative_momentum_tfs(
    matrix: Optional[Mapping[str, Any]],
    *,
    accelerating_only: bool = False,
    tfs: tuple[str, ...] = _INTRADAY_TFS,
) -> List[tuple[str, float]]:
    """回傳動能為負的日內框架 [(tf, momentum_value)]。

    accelerating_only=True 時另要求 `momentum_color == "Red"`（負且增強）。
    矩陣缺框架、欄位缺失或型別錯誤的框架一律略過。
    """
    out: List[tuple[str, float]] = []
    if not isinstance(matrix, Mapping):
        return out
    for tf in tfs:
        st = matrix.get(tf)
        if st is None:
            continue
        mv = _momentum_or_none(st)
        if mv is None or mv >= 0:
            continue
        if (
            accelerating_only
            and getattr(st, "momentum_color", None) != _ACCELERATING_COLOR
        ):
            continue
        out.append((tf, mv))
    return out


def assess_intraday_conflict(
    matrix: Optional[Mapping[str, Any]],
    *,
    bar_open: Optional[float] = None,
    bar_close: Optional[float] = None,
    rvol_eff: Optional[float] = None,
    lvn: Optional[float] = None,
) -> List[str]:
    """回傳日內衝突說明（空 list＝無衝突）。

    1. 65m 或 15m 動能為負且增強（Red）。
    2. 最新 15m 為實體陰線、量比 >= INTRADAY_BREAK_RVOL，且收盤跌穿 LVN
       （`bar_close < lvn <= bar_open`）。
    只給 matrix 時（前向紀錄）僅判第 1 條。
    """
    reasons: List[str] = []
    for tf, mv in negative_momentum_tfs(matrix, accelerating_only=True):
        reasons.append(f"{tf} 動能{mv:.2f}加速向下")
    if (
        bar_open is not None
        and bar_close is not None
        and rvol_eff is not None
        and lvn is not None
        and lvn > 0
        and bar_close < bar_open
        and rvol_eff >= INTRADAY_BREAK_RVOL
        and bar_close < lvn <= bar_open
    ):
        reasons.append(f"15m 放量陰線{rvol_eff:.2f}x破LVN ${lvn:.2f}")
    return reasons
