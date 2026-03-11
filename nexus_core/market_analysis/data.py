"""
財報日期查詢 — 透過 Finnhub Earnings Calendar API (Async)。
"""

import logging
import asyncio
from datetime import date
import pandas as pd
import yfinance as yf

from services import market_data_service

logger = logging.getLogger(__name__)

async def get_next_earnings_date(symbol: str):
    """取得下一次財報發布日期。"""
    try:
        earnings = await market_data_service.get_earnings_calendar(symbol)
        
        if not earnings: return None
        today = date.today()
        for entry in earnings:
            d_str = entry.get('date')
            if not d_str: continue
            try:
                d_date = date.fromisoformat(d_str)
            except (ValueError, TypeError): continue
            if d_date >= today: return d_date

        last_entry = earnings[-1]
        d_str = last_entry.get('date')
        if d_str:
            try: return date.fromisoformat(d_str)
            except (ValueError, TypeError): pass
        return None
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
