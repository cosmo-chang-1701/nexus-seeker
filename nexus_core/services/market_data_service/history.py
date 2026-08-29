"""market_data_service：歷史 K 線數據與衍生指標 (SMA/EMA)。"""

from typing import Optional
import asyncio
import logging
import time

import pandas as pd
import numpy as np
import yfinance as yf

from services.market_data_service._core import (
    _to_yfinance_symbol,
    _sanitize_ticker,
    call_yf,
)
from services.market_data_service.caches import (
    _ema_cache,
    _EMA_CACHE_TTL,
    _history_cache,
    _HISTORY_CACHE_TTL,
    _sma_cache,
    _SMA_CACHE_TTL,
)
from services.market_data_service.quote import _safe_yf_history

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# 歷史數據與指標 (yfinance)
# ---------------------------------------------------------------------------
async def get_history_df(
    symbol: str, period: str = "1y", interval: str = "1d", force_refresh: bool = False
) -> pd.DataFrame:
    """
    使用 yfinance 抓取歷史 K 線 (異步化，支援 4 小時快取與 Copy 隔離)。

    `force_refresh=True` 會略過快取讀取（但仍會將新結果寫入快取供其他呼叫端
    受益），供對資料新鮮度要求較高的短週期呼叫端使用（例如 15 分鐘價量警報）。
    """
    symbol = _to_yfinance_symbol(symbol)
    cache_key = (symbol, period, interval)
    now = time.time()

    if not force_refresh and cache_key in _history_cache:
        cached_df, expiry = _history_cache[cache_key]
        if now < expiry:
            return cached_df.copy()

    try:
        ticker = yf.Ticker(symbol)
        df = await _safe_yf_history(ticker, period=period, interval=interval)

        if df is None or getattr(df, "empty", True):
            logger.warning(
                f"[{symbol}] yfinance 歷史數據為空 (period={period}, interval={interval})"
            )
            empty_df = pd.DataFrame()
            _history_cache[cache_key] = (empty_df, now + _HISTORY_CACHE_TTL)
            return empty_df

        df.index.name = "Date"
        if df.index.tz is not None:
            df.index = df.index.tz_localize(None)

        result_df = df[["Open", "High", "Low", "Close", "Volume"]]
        _history_cache[cache_key] = (result_df.copy(), now + _HISTORY_CACHE_TTL)
        return result_df
    except Exception as e:
        logger.error(f"[{symbol}] yfinance 抓取失敗: {e}")
        return pd.DataFrame()


async def get_spy_history_df(
    period: str = "1y", interval: str = "1d", retries: int = 3
) -> pd.DataFrame:
    """取得 SPY 基準歷史資料，針對暫時性鎖衝突進行重試。"""
    for attempt in range(retries):
        df = await get_history_df("SPY", period=period, interval=interval)
        if not df.empty:
            return df
        await asyncio.sleep(0.4 * (attempt + 1))

    logger.error(f"[SPY] 重試 {retries} 次後仍無法取得歷史資料")
    return pd.DataFrame()


async def get_stock_splits(symbol: str) -> pd.Series:
    """取得標的的拆股歷史資料。"""
    symbol = _sanitize_ticker(symbol)
    try:
        ticker = yf.Ticker(symbol)
        splits = await call_yf(lambda: ticker.splits)
        if splits is None:
            return pd.Series(dtype=float)
        return splits
    except Exception as e:
        logger.error(f"[{symbol}] yfinance 獲取拆股歷史失敗: {e}")
        return pd.Series(dtype=float)


async def get_sma(symbol: str, window: int = 200) -> Optional[float]:
    """計算簡單移動平均線 (SMA)。"""
    current_time = time.time()
    cache_key = (symbol, window)

    if cache_key in _sma_cache:
        cached_val, expiry = _sma_cache[cache_key]
        if current_time < expiry:
            return cached_val  # type: ignore

    try:
        period = "1y" if window <= 200 else "2y"
        df = await get_history_df(symbol, period=period)

        if df.empty or len(df) < window:
            return None

        sma_series = df["Close"].rolling(window=window).mean()
        current_sma = round(float(sma_series.iloc[-1]), 4)

        if not pd.isna(current_sma):
            _sma_cache[cache_key] = (current_sma, current_time + _SMA_CACHE_TTL)

        return current_sma if not pd.isna(current_sma) else None
    except Exception as e:
        logger.error(f"[{symbol}] 計算 SMA{window} 失敗: {e}")
        return None


async def get_ema(symbol: str, window: int = 21) -> Optional[float]:
    """計算指數移動平均線 (EMA)。"""
    now = time.time()
    cache_key = (symbol, window)

    if cache_key in _ema_cache:
        val, expiry = _ema_cache[cache_key]
        if now < expiry:
            return val  # type: ignore

    try:
        period = "1mo" if window <= 21 else "1y"
        df = await get_history_df(symbol, period=period)

        if df.empty or len(df) < window:
            return None

        ema_series = df["Close"].ewm(span=window, adjust=False).mean()
        current_ema = round(float(ema_series.iloc[-1]), 4)

        if not np.isnan(current_ema):
            _ema_cache[cache_key] = (current_ema, now + _EMA_CACHE_TTL)
        return current_ema if not np.isnan(current_ema) else None
    except Exception as e:
        logger.error(f"[{symbol}] EMA{window} 計算失敗: {e}")
        return None
