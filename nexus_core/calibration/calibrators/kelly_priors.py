"""凱利勝率先驗校準 (side × RSI 桶)。

勝率定義為 1.8:1 非對稱屏障 (目標 1.8×ATR₁D、停損 1.0×ATR₁D) 的先觸及機率，
與 KELLY_PRIOR_ODDS = 1.8 對應。**只用窗口內已分勝負的樣本**：凱利公式假設的是
「贏 b 或輸 1」的二元賭局，把 5 個交易日內兩邊都沒碰到的逾時當成輸，會系統性
壓低勝率。逾時比例另列於報告供判讀。

提案用 Wilson 下界再向現行值收縮；護欄：做空提案 <= 同桶做多提案。
賠率 (odds) 不提案：只報告，理由見文件。
"""

import pandas as pd

from calibration.config import CalibrationConfig
from calibration.parameter_registry import ParameterResult, new_result
from calibration.stats import is_sufficient, shrink, summarize

_BUCKETS = (("rsi_lt_50", 0.0, 50.0), ("rsi_ge_50", 50.0, 100.01))


def calibrate(labeled: pd.DataFrame, cfg: CalibrationConfig) -> list[ParameterResult]:
    results: dict[str, ParameterResult] = {}
    base = (
        labeled[labeled["event_type"] != "RANDOM_CONTROL"]
        if not labeled.empty
        else labeled
    )
    for side in ("LONG", "SHORT"):
        for label, lo, hi in _BUCKETS:
            name = f"kelly_prior.{side}.{label}"
            res = new_result(name, "1.8:1 屏障勝率 Wilson 下界，向現行值收縮")
            bucket = (
                base[(base["side"] == side) & (base["rsi"] >= lo) & (base["rsi"] < hi)]
                if not base.empty
                else base
            )
            if bucket.empty:
                res.notes.append("無樣本")
                results[name] = res
                continue
            resolved = bucket[bucket["win_rr18"].notna()]
            timeout_share = 1.0 - len(resolved) / len(bucket)
            res.notes.append(f"逾時 (未分勝負) 比例 {timeout_share:.1%}，已排除")
            if resolved.empty:
                res.notes.append("INSUFFICIENT — 維持預設")
                results[name] = res
                continue
            resolved = resolved.assign(win_rr18=resolved["win_rr18"].astype(int))
            st = summarize(
                resolved, cfg.n_boot, cfg.seed, cfg.oos_split, win_col="win_rr18"
            )
            res.n, res.n_dates, res.oos_agrees = st.n, st.n_dates, st.oos_agrees
            res.estimate = st.win_rate
            res.ci95 = (st.wilson_lo, st.wilson_hi)
            res.sufficient = is_sufficient(st, cfg.min_events, cfg.min_dates)
            if res.sufficient:
                res.proposed = round(
                    shrink(float(res.current), st.wilson_lo, st.n, cfg.shrinkage_n0), 3
                )
            else:
                res.notes.append("INSUFFICIENT — 維持預設")
            results[name] = res
    for label, _lo, _hi in _BUCKETS:
        short = results[f"kelly_prior.SHORT.{label}"]
        long_ = results[f"kelly_prior.LONG.{label}"]
        if float(short.proposed) > float(long_.proposed):
            short.proposed = long_.proposed
            short.guardrail_applied = "SHORT_CLAMPED_TO_LONG"
            short.notes.append(
                "護欄：做空先驗不得高於同 RSI 桶的做多建議值 (kelly_priors 結構性夾制)"
            )
    return list(results.values())
