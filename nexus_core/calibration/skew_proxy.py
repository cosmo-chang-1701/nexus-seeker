"""Skew 百分位門檻的代理校準（CBOE SKEW 指數 × SPY 事後走勢）。

為什麼用代理：系統的 25-Delta Skew 沒有可下載的歷史（需要逐日歷史期權鏈），
日級規範母體 (`sentiment_daily_canonical`) 要從上線日起才開始累積。CBOE SKEW
(^SKEW) 是 S&P 500 價外選擇權的尾部風險定價指數，量測的是同一件事（下檔保護的
相對昂貴程度），並有 1990 年至今的日線，可以回答兩個問題：

1. 以「日級、252 交易日、midrank」母體計算時，各門檻的實際觸發率是多少？
2. 高分位日之後，SPY 的 5 日報酬與下跌風險是否顯著異於基準？

限制（報告中會揭露）：^SKEW 只有指數層級，個股 Skew 的分布與尾部行為不同；
這份結果只能作為門檻量級的先驗，最終值仍須以前向蒐集的個股資料確認。

只寫 `cache_dir` 與報告目錄，不寫資料庫、不改程式碼。
"""

import logging
import random
from bisect import bisect_left, bisect_right, insort
from pathlib import Path
from typing import Any, Optional

from calibration.data_store import save_frame

logger = logging.getLogger(__name__)

WINDOW = 252
MIN_SAMPLES = 60
HORIZON = 5
THRESHOLDS_HIGH: tuple[float, ...] = (80.0, 82.0, 85.0, 88.0, 90.0, 95.0, 97.5, 98.0)
THRESHOLDS_LOW: tuple[float, ...] = (20.0, 15.0, 10.0, 5.0)
# 事後大跌定義：5 日內最低收盤相對當日收盤跌幅超過此值
DRAWDOWN_EVENT = -0.03


def rolling_midrank_percentile(
    values: list[float], window: int = WINDOW
) -> list[Optional[float]]:
    """第 t 天的分位只以 t 之前的 `window` 天為母體（不含當天，無前視偏差）。"""
    out: list[Optional[float]] = []
    pool: list[float] = []
    for t, x in enumerate(values):
        if len(pool) >= MIN_SAMPLES:
            lo = bisect_left(pool, x)
            hi = bisect_right(pool, x)
            out.append((lo + 0.5 * (hi - lo)) / len(pool) * 100.0)
        else:
            out.append(None)
        insort(pool, x)
        if len(pool) > window:
            old = values[t - window]
            pool.pop(bisect_left(pool, old))
    return out


def _bootstrap_ci(xs: list[float], seed: int = 7, n: int = 2000) -> tuple[float, float]:
    if len(xs) < 2:
        return (float("nan"), float("nan"))
    rng = random.Random(seed)
    means = sorted(sum(rng.choice(xs) for _ in xs) / len(xs) for _ in range(n))
    return (means[int(0.025 * n)], means[int(0.975 * n)])


def _event_stats(
    mask: list[bool], fwd_ret: list[Optional[float]], fwd_dd: list[Optional[float]]
) -> dict[str, Any]:
    rets = [r for m, r in zip(mask, fwd_ret) if m and r is not None]
    dds = [d for m, d in zip(mask, fwd_dd) if m and d is not None]
    if not rets:
        return {"n_days": 0}
    lo, hi = _bootstrap_ci(rets)
    return {
        "n_days": len(rets),
        "fwd5d_mean_ret": round(sum(rets) / len(rets), 5),
        "fwd5d_ci95": (round(lo, 5), round(hi, 5)),
        "p_drawdown_gt_3pct": round(
            sum(1 for d in dds if d <= DRAWDOWN_EVENT) / len(dds), 4
        ),
    }


def run_skew_proxy(cache_dir: Path, since: str = "2000-01-01") -> dict[str, Any]:
    import yfinance as yf

    skew = yf.Ticker("^SKEW").history(start=since, interval="1d")["Close"]
    spy = yf.Ticker("SPY").history(start=since, interval="1d", auto_adjust=True)[
        "Close"
    ]
    skew.index = skew.index.tz_localize(None).normalize()
    spy.index = spy.index.tz_localize(None).normalize()
    df = skew.to_frame("skew").join(spy.to_frame("spy"), how="inner").dropna()
    df = df[df["skew"] > 0]

    save_frame(Path(cache_dir) / "skew_proxy" / "skew_spy_daily.csv", df)

    skew_vals = [float(v) for v in df["skew"]]
    closes = [float(v) for v in df["spy"]]
    pct = rolling_midrank_percentile(skew_vals)

    n = len(closes)
    fwd_ret: list[Optional[float]] = []
    fwd_dd: list[Optional[float]] = []
    for t in range(n):
        if t + HORIZON >= n:
            fwd_ret.append(None)
            fwd_dd.append(None)
            continue
        c0 = closes[t]
        fwd_ret.append(closes[t + HORIZON] / c0 - 1.0)
        fwd_dd.append(min(closes[t + 1 : t + HORIZON + 1]) / c0 - 1.0)

    valid = [p is not None for p in pct]
    base = _event_stats(valid, fwd_ret, fwd_dd)

    high: dict[str, Any] = {}
    for th in THRESHOLDS_HIGH:
        mask = [p is not None and p >= th for p in pct]
        st = _event_stats(mask, fwd_ret, fwd_dd)
        st["fire_rate"] = round(sum(mask) / max(1, sum(valid)), 4)
        high[f">={th}"] = st
    low: dict[str, Any] = {}
    for th in THRESHOLDS_LOW:
        mask = [p is not None and p <= th for p in pct]
        st = _event_stats(mask, fwd_ret, fwd_dd)
        st["fire_rate"] = round(sum(mask) / max(1, sum(valid)), 4)
        low[f"<={th}"] = st

    # 連續觸發：同一段高分位行情會連續多天觸發，事件數遠少於天數
    def _episodes(th: float) -> int:
        count, prev = 0, False
        for p in pct:
            cur = p is not None and p >= th
            if cur and not prev:
                count += 1
            prev = cur
        return count

    return {
        "source": "CBOE ^SKEW (S&P 500 尾部風險指數) × SPY",
        "period": f"{df.index[0].date()} ~ {df.index[-1].date()}",
        "n_days": int(sum(valid)),
        "window": WINDOW,
        "horizon_days": HORIZON,
        "baseline": base,
        "high_percentile": high,
        "low_percentile": low,
        "episodes_high": {f">={th}": _episodes(th) for th in THRESHOLDS_HIGH},
        "caveat": (
            "指數層級代理；個股 Skew 的分布與尾部行為不同，結果只作為門檻量級的先驗，"
            "最終值須以前向蒐集的個股資料確認。"
        ),
    }
