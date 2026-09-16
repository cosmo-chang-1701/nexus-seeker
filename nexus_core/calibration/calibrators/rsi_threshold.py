"""Regime V RSI 上限掃描 (_REGIME_V_RSI_MAX)。

訓練期挑「期望值 CI 下界最大」的 θ，樣本外確認方向一致才提案。報告揭露比較次數
(多重比較：格點越多，訓練期最佳值越可能只是雜訊)。
"""

import pandas as pd

from calibration.config import CalibrationConfig
from calibration.events import first_per_session
from calibration.parameter_registry import ParameterResult, new_result
from calibration.stats import effective_oos_split, summarize


def calibrate(labeled: pd.DataFrame, cfg: CalibrationConfig) -> list[ParameterResult]:
    res = new_result("_REGIME_V_RSI_MAX", "訓練期 argmax(期望值 CI 下界)，樣本外確認")
    base = (
        labeled[labeled["event_type"] == "REGIME_V_PROXY"]
        if not labeled.empty
        else labeled
    )
    if base.empty:
        res.notes.append("無 REGIME_V_PROXY 樣本")
        return [res]
    split = effective_oos_split(base["date"], cfg.oos_split)
    best = None
    rows = []
    for theta in cfg.rsi_grid:
        subset = first_per_session(base[base["rsi"] < theta])
        train = subset[subset["date"] < split]
        if len(train) < cfg.min_events:
            rows.append(f"θ={theta:g}: 訓練樣本 {len(train)} 不足")
            continue
        st = summarize(train, cfg.n_boot, cfg.seed, "0000-00-00")
        rows.append(
            f"θ={theta:g}: n={st.n} 期望值 {st.expectancy:+.3f}R 下界 {st.exp_lo:+.3f}"
        )
        if best is None or st.exp_lo > best[1].exp_lo:
            best = (theta, st, subset)
    res.notes.extend(rows)
    res.notes.append(f"多重比較：共 {len(cfg.rsi_grid)} 個格點")
    if best is None:
        res.notes.append("INSUFFICIENT — 維持預設")
        return [res]
    theta, train_st, subset = best
    test = subset[subset["date"] >= split]
    res.estimate = float(theta)
    res.n = int(len(subset))
    res.n_dates = int(subset["date"].nunique())
    test_exp = float(test["r_multiple"].mean()) if len(test) else None
    res.oos_agrees = bool(
        test_exp is not None and train_st.expectancy > 0 and test_exp > 0
    )
    res.ci95 = (train_st.exp_lo, train_st.exp_hi)
    res.notes.append(
        f"樣本外 ({split} 起) n={len(test)} 期望值 "
        + (f"{test_exp:+.3f}R" if test_exp is not None else "N/A")
    )
    res.sufficient = (
        res.n >= cfg.min_events and res.n_dates >= cfg.min_dates and res.oos_agrees
    )
    if res.sufficient:
        res.proposed = theta
    else:
        res.notes.append("INSUFFICIENT — 維持預設")
    return [res]
