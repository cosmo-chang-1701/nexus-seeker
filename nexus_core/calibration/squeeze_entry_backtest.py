"""多時間框架擠壓進場：日線事件研究（離線）。

問題：擠壓規則給出的等級（T1／T2／T3），之後的報酬是否優於「任意一天買進」？
這是 B&H 使用者唯一關心的擇時問題——不是要不要持有，而是「這一天進場」有沒有
比隨機一天好。

方法（無前視）：
* 第 t 日收盤後，以第 t 日（含）以前「已收盤」的 W／3D／D K 棒建立擠壓矩陣，
  呼叫 production 的 `squeeze_entry.rules.evaluate_squeeze_entry` 判定等級。
* 進場價為第 t+1 日開盤，出場為第 t+h 日收盤（h = 5／20／60）。
* 基準為同一標的「所有交易日」以同樣方式進出的報酬；超額 = 訊號報酬 − 該標的
  同期間的無條件平均報酬（扣掉個股本身的漂移，避免強勢股灌水）。
* 獨立事件：同一標的同一等級在 `cooldown` 個交易日內只計第一次（重疊持有期
  會把同一段行情重複計數）。

已知限制（報告須揭露）：
* 只有日線資料：65m／15m／5m 時間框架無歷史，T1（需 15m/5m 觸發）只能透過
  「壓力區突破」觸發，T2 的「≥3 個時間框架擠壓」只能由 W／3D／D 湊滿——兩者
  都比 production 嚴格，盤中路徑只能靠前向紀錄驗證。
* 無財報日歷史，不套用財報否決；無宏觀逃頂歷史，不套用降級。
* 選股池為快取內既有標的，事後挑選，報酬水準會高估；看「相對無條件基準」的
  差異，而不是絕對報酬。

本模組不修改任何 production 程式碼或參數。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, Iterable, List, Optional, Tuple

import numpy as np
import pandas as pd

from market_analysis.psq_engine import compute_psq_series
from market_analysis.squeeze_entry.resistance import detect_resistance
from market_analysis.squeeze_entry.rules import evaluate_squeeze_entry
from market_analysis.squeeze_entry.timeframes import (
    GREEN_DOT_LOOKBACK,
    TimeframeState,
)

HORIZONS: Tuple[int, ...] = (5, 20, 60)
_RESISTANCE_WINDOW = 80
_ATR_LENGTH = 14


# ---------------------------------------------------------------------------
# 重採樣（以「完成時點」對齊到日線）
# ---------------------------------------------------------------------------
def _ohlc_agg(df: pd.DataFrame, keys: pd.Series) -> pd.DataFrame:
    g = df.groupby(keys, sort=True)
    out = g.agg({"Open": "first", "High": "max", "Low": "min", "Close": "last"})
    out["_last_date"] = g.apply(lambda x: x.index[-1])
    out["_count"] = g.size()
    return out


def weekly_bars(daily: pd.DataFrame) -> pd.DataFrame:
    """週線（週五為界），索引為該週最後一個交易日（＝該週線完成的時點）。"""
    week_end = pd.Series(
        [
            ts + pd.offsets.Week(weekday=4) if ts.weekday() != 4 else ts
            for ts in daily.index
        ],
        index=daily.index,
    ).dt.normalize()
    w = _ohlc_agg(daily, week_end)
    w.index = pd.DatetimeIndex(w["_last_date"])
    return w.drop(columns=["_last_date", "_count"])


def three_day_bars(daily: pd.DataFrame) -> pd.DataFrame:
    """3D 線：以資料內交易日序號 // 3 分組，丟掉未滿 3 日的最後一組。"""
    keys = pd.Series(np.arange(len(daily)) // 3, index=daily.index)
    t = _ohlc_agg(daily, keys)
    t = t[t["_count"] == 3]
    t.index = pd.DatetimeIndex(t["_last_date"])
    return t.drop(columns=["_last_date", "_count"])


def align_completed(
    series_frame: pd.DataFrame, daily_index: pd.DatetimeIndex
) -> pd.DataFrame:
    """高時間框架序列對齊到日線：第 t 日取「完成時點 <= t」的最新一根。"""
    return series_frame.reindex(daily_index, method="ffill")


# ---------------------------------------------------------------------------
# 逐日判定
# ---------------------------------------------------------------------------
_COLS = (
    "squeeze_level",
    "is_squeezing",
    "momentum_value",
    "momentum_color",
    "green_dot",
    "turbo",
    "sma_20",
)


def build_daily_signals(
    daily: pd.DataFrame, start: Optional[str] = None
) -> pd.DataFrame:
    """逐日回傳 status／tier／triggers 等欄位（索引為日線日期）。"""
    daily = daily.dropna(subset=["Open", "High", "Low", "Close"]).copy()
    d_ser = compute_psq_series(daily, green_dot_lookback=GREEN_DOT_LOOKBACK["D"])
    w_raw = weekly_bars(daily)
    t_raw = three_day_bars(daily)
    w_ser = compute_psq_series(w_raw, green_dot_lookback=GREEN_DOT_LOOKBACK["W"])
    t_ser = compute_psq_series(t_raw, green_dot_lookback=GREEN_DOT_LOOKBACK["3D"])
    if d_ser is None or w_ser is None or t_ser is None:
        return pd.DataFrame()

    idx = daily.index
    frames = {
        "D": d_ser,
        "W": align_completed(w_ser, idx),
        "3D": align_completed(t_ser, idx),
    }
    import pandas_ta as ta

    atr = ta.atr(daily["High"], daily["Low"], daily["Close"], length=_ATR_LENGTH)

    rows: List[Dict[str, object]] = []
    positions = range(len(idx))
    if start is not None:
        first = int(idx.searchsorted(pd.Timestamp(start)))
        positions = range(max(first, _RESISTANCE_WINDOW), len(idx))
    for i in positions:
        matrix: Dict[str, TimeframeState] = {}
        for tf, fr in frames.items():
            r = fr.iloc[i]
            if pd.isna(r["momentum_value"]) or pd.isna(r["sma_20"]):
                continue
            matrix[tf] = TimeframeState(
                timeframe=tf,
                squeeze_level=str(r["squeeze_level"]),
                is_squeezing=bool(r["is_squeezing"]),
                momentum_value=float(r["momentum_value"]),
                momentum_color=str(r["momentum_color"]),
                green_dot=bool(r["green_dot"]),
                green_dot_bars_ago=None,
                turbo=bool(r["turbo"]),
                squeeze_range_low=None,
                sma_20=float(r["sma_20"]),
                last_close=float(daily["Close"].iloc[i]),
                bar_ts=str(idx[i]),
            )
        atr_i = (
            float(atr.iloc[i]) if atr is not None and not pd.isna(atr.iloc[i]) else 0.0
        )
        window = daily.iloc[max(0, i - _RESISTANCE_WINDOW + 1) : i + 1]
        spot = float(daily["Close"].iloc[i])
        res = detect_resistance(window, spot, atr_i)
        result = evaluate_squeeze_entry(matrix, res)
        rows.append(
            {
                "date": idx[i],
                "status": result.status,
                "tier": result.tier if result.status == "ENTRY" else None,
                "pending_tier": result.tier
                if result.status == "PENDING_BREAKOUT"
                else None,
                "squeeze_count": result.squeeze_count,
                "triggers": ",".join(result.triggers),
            }
        )
    return pd.DataFrame(rows).set_index("date") if rows else pd.DataFrame()


def forward_returns(
    daily: pd.DataFrame, horizons: Iterable[int] = HORIZONS
) -> pd.DataFrame:
    """第 t 列 = 第 t+1 日開盤進場、第 t+h 日收盤出場的報酬。"""
    entry = daily["Open"].shift(-1)
    out = {}
    for h in horizons:
        out[f"ret_{h}"] = daily["Close"].shift(-h) / entry - 1.0
    return pd.DataFrame(out, index=daily.index)


def dedupe_events(mask: pd.Series, cooldown: int) -> pd.Series:
    """同一序列中，事件後 `cooldown` 個交易日內的重複事件不計。"""
    keep = np.zeros(len(mask), dtype=bool)
    last = -(10**9)
    for i, v in enumerate(mask.to_numpy()):
        if v and i - last > cooldown:
            keep[i] = True
            last = i
    return pd.Series(keep, index=mask.index)


@dataclass
class BucketStats:
    label: str
    horizon: int
    n_events: int
    n_independent: int
    mean: float
    median: float
    hit_rate: float
    p10: float
    excess_mean: float  # 減去同標的無條件平均
    # 超額的 t 值：標準誤以「獨立事件數」估（重疊持有期的事件彼此高度相關，
    # 用總事件數會嚴重高估顯著性）。|t| < 2 視為與隨機進場無法區分。
    excess_t: float = float("nan")


def summarize(
    panel: pd.DataFrame, horizons: Iterable[int] = HORIZONS, cooldown: int = 10
) -> List[BucketStats]:
    """panel 欄位：symbol、status、tier、pending_tier、ret_h…（每列一個標的日，
    索引唯一且同一標的內依日期排序）。"""
    out: List[BucketStats] = []
    buckets = {
        "T1": panel["tier"] == 1,
        "T2": panel["tier"] == 2,
        "T3": panel["tier"] == 3,
        "任一等級 (ENTRY)": panel["status"] == "ENTRY",
        "待突破 (PENDING)": panel["status"] == "PENDING_BREAKOUT",
        "觀察 (WATCH)": panel["status"] == "WATCH",
        "全部交易日 (基準)": pd.Series(True, index=panel.index),
    }
    for h in horizons:
        col = f"ret_{h}"
        valid = panel[col].notna()
        sym_mean = panel.loc[valid].groupby("symbol")[col].mean()
        for label, mask in buckets.items():
            m = mask & valid
            sub = panel.loc[m]
            if sub.empty:
                out.append(
                    BucketStats(label, h, 0, 0, np.nan, np.nan, np.nan, np.nan, np.nan)
                )
                continue
            indep = 0
            for _, grp in panel.loc[valid].groupby("symbol"):
                indep += int(dedupe_events(mask.loc[grp.index], cooldown).sum())
            r = sub[col]
            excess = r - sub["symbol"].map(sym_mean)
            se = float(excess.std(ddof=1)) / np.sqrt(indep) if indep > 1 else np.nan
            t_val = float(excess.mean()) / se if se and se > 0 else np.nan
            out.append(
                BucketStats(
                    label,
                    h,
                    int(len(sub)),
                    indep,
                    float(r.mean()),
                    float(r.median()),
                    float((r > 0).mean()),
                    float(r.quantile(0.10)),
                    float(excess.mean()),
                    t_val,
                )
            )
    return out


def build_panel(
    dailies: Dict[str, pd.DataFrame], start: Optional[str] = None
) -> pd.DataFrame:
    parts = []
    for sym, daily in dailies.items():
        sig = build_daily_signals(daily, start)
        if sig.empty:
            continue
        fwd = forward_returns(daily).reindex(sig.index)
        part = sig.join(fwd)
        part["symbol"] = sym
        parts.append(part.reset_index())
    # 索引必須唯一：同一日期在不同標的各有一列，以日期當索引會讓按標的分組時
    # 以 .loc 取到其他標的的同日列。
    return pd.concat(parts, ignore_index=True) if parts else pd.DataFrame()


def render_markdown(stats: List[BucketStats], meta: Dict[str, object]) -> str:
    lines = ["# 多時間框架擠壓進場：日線事件研究", ""]
    for k, v in meta.items():
        lines.append(f"- {k}：{v}")
    lines.append("")
    for h in sorted({s.horizon for s in stats}):
        lines += [
            f"## 持有 {h} 個交易日（t+1 開盤進、t+{h} 收盤出）",
            "",
            "| 分組 | 事件數 | 獨立事件 | 平均 | 中位數 | 勝率 | P10 | 超額（減同標的平均） | 超額 t 值 |",
            "| :-- | --: | --: | --: | --: | --: | --: | --: | --: |",
        ]
        for s in stats:
            if s.horizon != h:
                continue
            if s.n_events == 0:
                lines.append(f"| {s.label} | 0 | 0 | – | – | – | – | – | – |")
                continue
            lines.append(
                f"| {s.label} | {s.n_events} | {s.n_independent} | {s.mean:+.2%} | "
                f"{s.median:+.2%} | {s.hit_rate:.1%} | {s.p10:+.2%} | {s.excess_mean:+.2%} | "
                f"{s.excess_t:+.2f} |"
            )
        lines.append("")
    return "\n".join(lines)
