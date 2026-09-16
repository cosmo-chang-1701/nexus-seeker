"""做空 VIX 倒 U 形乘數校準。

資料：方向性做空事件 (SCANNER_BTO_PUT 日線 + REGIME_V_PROXY 1h)，依進場前一交易日
^VIX 收盤分入六個階梯。

估計：tier_multiplier = clip(tier 期望值 / 全體做空期望值, 0, 1)。
全體期望值 <= 0 時整組不提案 (沒有正期望值可分配)。

護欄：
* 乘數夾在 [0, 1]——校準前的上限 1.0 是政策，不是統計結果。
* Extreme (VIX >= 35) 維持 0：即使 n >= 300 且期望值 CI 下界 > 0，也只標註
  「需人工政策覆核」，永不自動提案放寬。
"""

import pandas as pd

from calibration.config import CalibrationConfig
from calibration.parameter_registry import VIX_TIER_KEYS, ParameterResult, new_result
from calibration.stats import is_sufficient, shrink, summarize

_EXTREME_REVIEW_MIN_N = 300


def _tier_index(vix: float) -> int:
    from config import VIX_LADDER_CONFIG

    for i, tier in enumerate(VIX_LADDER_CONFIG):
        if tier["vix_floor"] <= vix < tier["vix_ceil"]:
            return i
    return len(VIX_LADDER_CONFIG) - 1


def calibrate(labeled: pd.DataFrame, cfg: CalibrationConfig) -> list[ParameterResult]:
    shorts = (
        labeled[
            (labeled["side"] == "SHORT")
            & labeled["event_type"].isin(["SCANNER_BTO_PUT", "REGIME_V_PROXY"])
            & labeled["vix_prev"].notna()
        ]
        if not labeled.empty
        else labeled
    )
    results: list[ParameterResult] = []
    pooled = (
        summarize(shorts, cfg.n_boot, cfg.seed, cfg.oos_split) if len(shorts) else None
    )
    tiers = shorts["vix_prev"].map(_tier_index) if len(shorts) else pd.Series(dtype=int)

    for i, key in enumerate(VIX_TIER_KEYS):
        res = new_result(
            f"short_vix_multiplier.{key}",
            "tier 期望值 / 全體做空期望值，clip [0,1]，向現行值收縮",
        )
        bucket = shorts[tiers == i] if len(shorts) else shorts
        if bucket is None or bucket.empty or pooled is None:
            res.notes.append("無樣本")
            results.append(res)
            continue
        st = summarize(bucket, cfg.n_boot, cfg.seed, cfg.oos_split)
        res.n, res.n_dates, res.oos_agrees = st.n, st.n_dates, st.oos_agrees
        if not (pooled.expectancy > 0):
            res.notes.append(
                f"全體做空期望值 {pooled.expectancy:+.3f}R <= 0，整組不提案"
            )
            results.append(res)
            continue
        ratio = st.expectancy / pooled.expectancy
        res.estimate = float(min(1.0, max(0.0, ratio)))
        res.ci95 = (
            float(min(1.0, max(0.0, st.exp_lo / pooled.expectancy))),
            float(min(1.0, max(0.0, st.exp_hi / pooled.expectancy))),
        )
        res.notes.append(
            f"tier 期望值 {st.expectancy:+.3f}R [{st.exp_lo:+.3f}, {st.exp_hi:+.3f}]，勝率 {st.win_rate:.1%}"
        )
        res.sufficient = is_sufficient(st, cfg.min_events, cfg.min_dates)
        if key == "extreme":
            res.proposed = res.current
            res.guardrail_applied = "EXTREME_LOCKED_AT_ZERO"
            if st.n >= _EXTREME_REVIEW_MIN_N and st.exp_lo > 0:
                res.notes.append(
                    "⚠️ 樣本顯示極端區做空期望值為正——需人工政策覆核，工具不提案放寬"
                )
            results.append(res)
            continue
        if res.sufficient:
            proposed = shrink(float(res.current), res.estimate, st.n, cfg.shrinkage_n0)
            clipped = min(1.0, max(0.0, proposed))
            if clipped != proposed:
                res.guardrail_applied = "CLIPPED_TO_[0,1]"
            res.proposed = round(clipped, 2)
        else:
            res.notes.append("INSUFFICIENT — 維持預設")
        results.append(res)
    return results
