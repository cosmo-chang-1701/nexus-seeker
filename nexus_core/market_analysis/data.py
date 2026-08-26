"""
財報日期查詢 — 透過 Finnhub Earnings Calendar API (Async)。
"""

from typing import Any
import logging
from datetime import date

from services.calendar_service import calendar_service

logger = logging.getLogger(__name__)


async def get_next_earnings_date(symbol: str) -> Any:
    """取得下一次財報發布日期。"""
    try:
        earnings_info = await calendar_service.get_symbol_earnings(symbol)
        if earnings_info is None:
            return None
        return date.fromisoformat(earnings_info.date)
    except Exception as e:
        logger.warning("取得財報日期失敗: %s", e)
        return None
