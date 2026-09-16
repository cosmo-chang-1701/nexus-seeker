"""校準工具測試共用的合成 K 線產生器 (非測試模組)。"""

from typing import Optional

import numpy as np
import pandas as pd


def synthetic_daily(
    n_days: int = 1600, seed: int = 1, start: str = "2017-01-02", drift: float = 0.0002
) -> pd.DataFrame:
    rng = np.random.default_rng(seed)
    idx = pd.bdate_range(start, periods=n_days, tz="America/New_York")
    rets = rng.normal(drift, 0.018, n_days)
    close = 100.0 * np.exp(np.cumsum(rets))
    open_ = close * np.exp(rng.normal(0, 0.004, n_days))
    high = np.maximum(open_, close) * (1 + np.abs(rng.normal(0, 0.008, n_days)))
    low = np.minimum(open_, close) * (1 - np.abs(rng.normal(0, 0.008, n_days)))
    vol = rng.integers(1_000_000, 3_000_000, n_days).astype(float)
    return pd.DataFrame(
        {"Open": open_, "High": high, "Low": low, "Close": close, "Volume": vol},
        index=idx,
    )


def synthetic_hourly(
    daily: pd.DataFrame, n_days: int = 400, seed: int = 2
) -> pd.DataFrame:
    """由日線最後 n_days 天展開成每天 7 根 1h K 棒 (開盤價銜接日線開盤)。"""
    rng = np.random.default_rng(seed)
    rows = []
    for ts, day in daily.iloc[-n_days:].iterrows():
        start = pd.Timestamp(ts).tz_convert(
            "America/New_York"
        ).normalize() + pd.Timedelta(hours=9, minutes=30)
        price = float(day["Open"])
        for h in range(7):
            ret = rng.normal(0, 0.006)
            o = price
            c = price * np.exp(ret)
            hi = max(o, c) * (1 + abs(rng.normal(0, 0.002)))
            lo = min(o, c) * (1 - abs(rng.normal(0, 0.002)))
            v = float(rng.integers(100_000, 400_000)) * (
                3.0 if rng.random() < 0.08 else 1.0
            )
            rows.append((start + pd.Timedelta(hours=h), o, hi, lo, c, v))
            price = c
    idx = pd.DatetimeIndex([r[0] for r in rows]).tz_convert("UTC")
    return pd.DataFrame(
        {
            "Open": [r[1] for r in rows],
            "High": [r[2] for r in rows],
            "Low": [r[3] for r in rows],
            "Close": [r[4] for r in rows],
            "Volume": [r[5] for r in rows],
        },
        index=idx,
    )


class FakeFetcher:
    """以合成資料取代 yfinance；記錄呼叫次數。"""

    def __init__(
        self,
        symbols: list[str],
        fail: Optional[set[str]] = None,
        fail_periods: Optional[set[str]] = None,
    ) -> None:
        self.calls: list[tuple[str, str, str]] = []
        self._fail = fail or set()
        self._fail_periods = fail_periods or set()
        self._daily = {s: synthetic_daily(seed=i + 10) for i, s in enumerate(symbols)}
        self._vix = synthetic_daily(seed=99, drift=0.0).assign(
            Close=lambda d: 12.0 + (d["Close"] % 30.0)
        )

    async def fetch(self, symbol: str, period: str, interval: str) -> pd.DataFrame:
        self.calls.append((symbol, period, interval))
        if symbol in self._fail or period in self._fail_periods:
            return pd.DataFrame()
        if symbol.startswith("^"):
            return self._vix
        daily = self._daily[symbol]
        if interval == "1d":
            return daily
        return synthetic_hourly(daily)
