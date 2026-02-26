"""
新聞服務 — 透過 Finnhub Company News API 取得標的近期新聞。
"""

import asyncio
import logging

from services import market_data_service

logger = logging.getLogger(__name__)


async def fetch_recent_news(symbol: str, limit: int = 5) -> str:
    """非同步獲取標的近期的新聞標題 (透過 Finnhub)"""
    try:
        def _get_news():
            news_items = market_data_service.get_company_news(symbol, limit=limit)
            if not news_items:
                return "近期無重大新聞。"
            
            lines = [
                f"▪️ {item.get('headline', 'No Title')}"
                for item in news_items
            ]
            return "\n".join(lines)
        
        return await asyncio.to_thread(_get_news)
    except Exception as e:
        logger.error(f"[{symbol}] 新聞獲取失敗: {e}")
        return "無法獲取近期新聞。"
