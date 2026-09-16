"""事件標註：共用 `market_analysis/outcome_labeling.py` (與 production 前向蒐集同一定義)，
再依事件方向轉為勝負與 R 倍數。"""

import math
from typing import Optional, Sequence

import numpy as np
import pandas as pd

from market_analysis.outcome_labeling import (
    TOUCH_NONE,
    directional_touch,
    first_barrier_touch,
    horizon_window,
    label_forward_path,
)

# 凱利先驗校準用的非對稱屏障：目標 1.8 × ATR₁D、停損 1.0 × ATR₁D，對應
# kelly_priors.KELLY_PRIOR_ODDS = 1.8。對稱屏障的勝率不能直接當成 1.8:1 賠率下的勝率。
KELLY_TARGET_ATR = 1.8
KELLY_STOP_ATR = 1.0
# 空間倍數校準的停損代理：2.5 × ATR₁₅ₘ (條件二緩衝下界)，ATR₁₅ₘ 以 ATR₁ₕ/2 近似；
# 日線事件無 ATR₁ₕ 時退回 1.0 × ATR₁D。
ROOM_STOP_ATR15_MULT = 2.5


def _directional_barrier(
    highs: list[float],
    lows: list[float],
    entry: float,
    side: str,
    favorable_dist: float,
    adverse_dist: float,
) -> int:
    """+1 有利先觸及、-1 不利先觸及 (同根雙觸算不利)、0 逾時。"""
    if side == "SHORT":
        upper, lower = entry + adverse_dist, entry - favorable_dist
    else:
        upper, lower = entry + favorable_dist, entry - adverse_dist
    touch, _ = first_barrier_touch(highs, lows, upper, lower)
    return directional_touch(touch, side)


def _resolved_win(outcome: int) -> Optional[int]:
    if outcome == 0:
        return None
    return 1 if outcome == 1 else 0


def label_events(
    events: pd.DataFrame,
    bars: pd.DataFrame,
    k_grid: Sequence[float],
    primary_k: float,
    max_sessions: int,
    room_grid: Sequence[float] = (),
) -> pd.DataFrame:
    """為單一標的、單一 K 線週期的事件逐筆標註。無法標註的事件被丟棄。"""
    if events.empty or bars is None or bars.empty:
        return pd.DataFrame()
    rows: list[dict[str, object]] = []
    horizon_days = pd.Timedelta(days=max_sessions * 2 + 4)
    for ev in events.itertuples(index=False):
        entry = float(ev.entry_price)
        atr = float(ev.atr_1d)
        if not (math.isfinite(entry) and entry > 0 and math.isfinite(atr) and atr > 0):
            continue
        signal_ts = pd.Timestamp(ev.signal_ts)
        sub = bars.loc[signal_ts : signal_ts + horizon_days]
        label = label_forward_path(sub, signal_ts, entry, atr, k_grid, max_sessions)
        if label is None:
            continue
        window = horizon_window(sub, signal_ts, max_sessions)
        highs = [float(v) for v in window["High"].tolist()]
        lows = [float(v) for v in window["Low"].tolist()]
        side = str(ev.side)
        d = -1.0 if side == "SHORT" else 1.0

        primary = next((t for t in label.touches if abs(t.k - primary_k) < 1e-9), None)
        if primary is None:
            continue
        outcome = directional_touch(primary.touch, side)
        if primary.touch == TOUCH_NONE:
            last_ret = label.fwd_ret_5d
            if last_ret is None:
                # 窗口未滿 5 日：以窗口內最後收盤計算，無資料則 0
                last_ret = (
                    float(window["Close"].iloc[-1]) / entry - 1.0
                    if not window.empty
                    else 0.0
                )
            r_multiple = float(
                np.clip(d * last_ret / (primary_k * atr / entry), -1.0, 1.0)
            )
        else:
            r_multiple = float(outcome)

        row: dict[str, object] = ev._asdict()
        row.update(
            {
                "outcome": outcome,
                "win": 1 if outcome == 1 else 0,
                "r_multiple": r_multiple,
                "signed_ret_1d": None
                if label.fwd_ret_1d is None
                else d * label.fwd_ret_1d,
                "signed_ret_5d": None
                if label.fwd_ret_5d is None
                else d * label.fwd_ret_5d,
                "mfe_atr": label.max_down_atr_5d
                if side == "SHORT"
                else label.max_up_atr_5d,
                "mae_atr": label.max_up_atr_5d
                if side == "SHORT"
                else label.max_down_atr_5d,
                # 1 勝 / 0 敗 / None 窗口內皆未觸及 (凱利校準只用已分勝負的樣本)
                "win_rr18": _resolved_win(
                    _directional_barrier(
                        highs,
                        lows,
                        entry,
                        side,
                        KELLY_TARGET_ATR * atr,
                        KELLY_STOP_ATR * atr,
                    )
                ),
            }
        )
        atr15 = (
            float(ev.atr_15m_equiv) if ev.atr_15m_equiv is not None else float("nan")
        )
        stop_dist = (
            ROOM_STOP_ATR15_MULT * atr15
            if math.isfinite(atr15) and atr15 > 0
            else KELLY_STOP_ATR * atr
        )
        row["room_stop_dist"] = stop_dist
        for m in room_grid:
            row[f"room_hit_{m:g}"] = _directional_barrier(
                highs, lows, entry, side, m * atr, stop_dist
            )
        rows.append(row)
    return pd.DataFrame(rows)
