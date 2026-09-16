"""空間倍數校準 (_ROOM_ATR_1D_MULTIPLIER / _BREAKDOWN_NEXT_STRIKE_ATR_1D_MULTIPLIER)。

對每個 m：目標 = m × ATR₁D、停損代理 = 2.5 × ATR₁₅ₘ (條件二緩衝下界)，計算
先觸及目標的勝率 P(m)，並換算為 R 期望值 EV(m) = P·r − (1−P)，r = m×ATR₁D / 停損距離。

提案：Wilson 下界代入後 EV > 0 的最小 m (「空間要求從哪裡開始值得」)。
這是啟發式，不是最佳化——報告同時列出整條曲線供人工判讀。
_ROOM_ABSOLUTE_FLOOR_PCT 屬風險政策，只報告、不提案。
"""

import math

import pandas as pd

from calibration.config import CalibrationConfig
from calibration.parameter_registry import ParameterResult, new_result
from calibration.stats import effective_oos_split, wilson_interval


def _curve(events: pd.DataFrame, cfg: CalibrationConfig, res: ParameterResult) -> None:
    split = effective_oos_split(events["date"], cfg.oos_split)
    candidate = None
    for m in cfg.room_grid:
        col = f"room_hit_{m:g}"
        if col not in events:
            continue
        n = len(events)
        wins = int((events[col] == 1).sum())
        p_lo, _p_hi = wilson_interval(wins, n)
        r = (m * events["atr_1d"] / events["room_stop_dist"]).median()
        if not math.isfinite(r) or r <= 0:
            continue
        ev_lo = p_lo * r - (1 - p_lo)
        oos = events[events["date"] >= split]
        oos_p = float((oos[col] == 1).mean()) if len(oos) else float("nan")
        res.notes.append(
            f"m={m:g}: P={wins / n:.1%} (下界 {p_lo:.1%})，r≈{r:.2f}，EV下界 {ev_lo:+.3f}R，樣本外 P={oos_p:.1%}"
        )
        if candidate is None and ev_lo > 0:
            oos_ev = oos_p * r - (1 - oos_p) if math.isfinite(oos_p) else float("nan")
            candidate = (m, ev_lo, oos_ev > 0)
    res.n = int(len(events))
    res.n_dates = int(events["date"].nunique())
    if candidate is None:
        res.notes.append("格點內無 EV 下界 > 0 的 m，INSUFFICIENT — 維持預設")
        return
    m, ev_lo, oos_ok = candidate
    res.estimate = float(m)
    res.oos_agrees = bool(oos_ok)
    res.sufficient = (
        res.n >= cfg.min_events and res.n_dates >= cfg.min_dates and res.oos_agrees
    )
    if res.sufficient:
        res.proposed = m
    else:
        res.notes.append("INSUFFICIENT — 維持預設")


def calibrate(labeled: pd.DataFrame, cfg: CalibrationConfig) -> list[ParameterResult]:
    long_res = new_result(
        "_ROOM_ATR_1D_MULTIPLIER", "EV 下界 > 0 的最小 ATR₁D 倍數 (REGIME_III_PROXY)"
    )
    chase_res = new_result(
        "_BREAKDOWN_NEXT_STRIKE_ATR_1D_MULTIPLIER",
        "EV 下界 > 0 的最小 ATR₁D 倍數 (REGIME_V_PROXY)",
    )
    floor_res = new_result("_ROOM_ABSOLUTE_FLOOR_PCT", "風險政策，只報告不提案")
    floor_res.notes.append("絕對底線屬風險政策，工具不提案")
    if labeled.empty:
        return [long_res, chase_res, floor_res]
    for res, etype in ((long_res, "REGIME_III_PROXY"), (chase_res, "REGIME_V_PROXY")):
        events = labeled[labeled["event_type"] == etype]
        if events.empty:
            res.notes.append(f"無 {etype} 樣本")
            continue
        _curve(events, cfg, res)
    return [long_res, chase_res, floor_res]
