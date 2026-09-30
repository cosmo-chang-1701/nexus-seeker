"""日線回測共用的資料面板：抓取、以 SPY 交易日曆對齊的 (開盤, 收盤)、VOO／BOXX 代理銜接。

供 `scripts/run_regime_momentum_backtest.py`（大盤三態 + 動能輪動）與
`scripts/run_static_allocation_backtest.py`（固定比例 + 定期再平衡）共用，兩者的資料
處理因此逐位元一致。只讀寫 `.calibration_cache/`，不碰 production DB。
"""

from __future__ import annotations

from collections.abc import Iterable

import pandas as pd

from calibration.data_store import DataStore
from calibration.regime_momentum_backtest import (
    CASH_PROXY,
    CASH_SYMBOL,
    CORE_PROXY,
    CORE_SYMBOL,
    MARKET_SYMBOL,
    build_cash_index,
    chain_returns,
)

FETCH_FROM = "2005-01-01"


async def fetch_daily(
    store: DataStore, symbols: Iterable[str], fetch_from: str = FETCH_FROM
) -> None:
    from calibration.fetcher import YFinanceFetcher

    fetcher = YFinanceFetcher()
    for sym in symbols:
        df = await fetcher.fetch(sym, "max", "1d")
        if not df.empty:
            idx = pd.to_datetime(df.index, utc=True)
            df = df[idx >= pd.Timestamp(fetch_from, tz="UTC")]
        n = store.save("1d", sym, df)
        print(f"  {sym}: {n} 列", flush=True)


def to_dates(df: pd.DataFrame) -> pd.DataFrame:
    """時間戳轉為美東交易日（tz-naive 日期），同日重複列取最後一筆。"""
    idx = pd.DatetimeIndex(df.index).tz_convert("America/New_York")
    out = df.copy()
    out.index = pd.DatetimeIndex(idx.tz_localize(None).normalize())
    return out[~out.index.duplicated(keep="last")]


def load_panel(
    store: DataStore, symbols: Iterable[str]
) -> tuple[pd.DataFrame, pd.DataFrame]:
    """以 SPY 的交易日曆對齊所有標的的 (開盤, 收盤)。"""
    frames: dict[str, pd.DataFrame] = {}
    for sym in symbols:
        df = store.load("1d", sym)
        if df is None or df.empty:
            continue
        frames[sym] = to_dates(df).astype(float)
    calendar = frames[MARKET_SYMBOL].index
    opens = pd.DataFrame({s: f["Open"] for s, f in frames.items()}).reindex(calendar)
    closes = pd.DataFrame({s: f["Close"] for s, f in frames.items()}).reindex(calendar)
    return opens, closes


def prepare_panel(
    opens: pd.DataFrame,
    closes: pd.DataFrame,
    fallback_cash_rate: float,
    required: Iterable[str] = (),
) -> tuple[pd.DataFrame, pd.DataFrame, pd.Series]:
    """VOO 上市前以 SPY 串接；建立 BOXX 現金指數；`required` 中缺資料的標的補 NaN 欄。"""
    opens = opens.copy()
    closes = closes.copy()
    closes[CORE_SYMBOL] = chain_returns(closes[CORE_SYMBOL], closes[CORE_PROXY])
    opens[CORE_SYMBOL] = chain_returns(opens[CORE_SYMBOL], opens[CORE_PROXY])
    cash_index = build_cash_index(
        closes.index,
        closes.get(CASH_SYMBOL),
        closes.get(CASH_PROXY),
        fallback_cash_rate,
    )
    for sym in required:
        if sym not in closes.columns:
            closes[sym] = float("nan")
            opens[sym] = float("nan")
    return opens, closes, cash_index
