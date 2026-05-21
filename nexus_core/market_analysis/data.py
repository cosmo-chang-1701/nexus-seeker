"""
財報日期查詢 — 透過 Finnhub Earnings Calendar API (Async)。
"""

import logging
from datetime import date
import pandas as pd
import yfinance as yf

from services.calendar_service import calendar_service

logger = logging.getLogger(__name__)


async def get_next_earnings_date(symbol: str):
    """取得下一次財報發布日期。"""
    try:
        earnings_info = await calendar_service.get_symbol_earnings(symbol)
        if earnings_info is None:
            return None
        return date.fromisoformat(earnings_info.date)
    except Exception as e:
        logger.warning("取得財報日期失敗: %s", e)
        return None


def get_option_chain(symbol: str, expiry: str):
    """透過 yfinance 獲取選擇權鏈 (同步)。"""
    try:
        ticker = yf.Ticker(symbol)
        chain = ticker.option_chain(expiry)
        return chain.calls, chain.puts
    except Exception as e:
        logger.warning(f"獲取選擇權鏈失敗 ({symbol}, {expiry}): {e}")
        return pd.DataFrame(), pd.DataFrame()
