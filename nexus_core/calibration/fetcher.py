"""歷史 K 線抓取器。沿用 `services.market_data_service.get_history_df`，因此自動
繼承 edge 代理降級 (datacenter IP 被 Yahoo 擋時)。"""

import asyncio
import logging
import random
from typing import Optional, Protocol

import pandas as pd

logger = logging.getLogger(__name__)


class HistoryFetcher(Protocol):
    async def fetch(self, symbol: str, period: str, interval: str) -> pd.DataFrame: ...


class YFinanceFetcher:
    """逐一請求、隨機間隔、指數退避重試。空結果視為失敗重試。"""

    def __init__(
        self, sleep_range: tuple[float, float] = (1.5, 3.0), retries: int = 3
    ) -> None:
        self._sleep_range = sleep_range
        self._retries = retries
        self._first = True

    async def fetch(self, symbol: str, period: str, interval: str) -> pd.DataFrame:
        from services.market_data_service import get_history_df

        if not self._first:
            await asyncio.sleep(random.uniform(*self._sleep_range))
        self._first = False
        last_error: Optional[Exception] = None
        for attempt in range(self._retries):
            try:
                df = await get_history_df(symbol, period=period, interval=interval)
                if df is not None and not df.empty:
                    return df
            except Exception as e:  # yfinance 例外型別不穩定
                last_error = e
            await asyncio.sleep(2.0**attempt)
        logger.warning(
            f"[calibration] {symbol} {interval}/{period} 抓取失敗: {last_error or 'empty'}"
        )
        return pd.DataFrame()
