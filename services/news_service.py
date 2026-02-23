import asyncio
import logging
import yfinance as yf

logger = logging.getLogger(__name__)

async def fetch_recent_news(symbol: str) -> str:
    """非同步獲取標的近期的 Yahoo Finance 新聞標題與摘要"""
    try:
        def _get_news():
            ticker = yf.Ticker(symbol)
            news_items = ticker.news
            if not news_items:
                return "近期無重大新聞。"
            
            # 使用列表推導式與 join 優化效能，並修正字典存取方式
            lines = [
                f"▪️ {item.get('content', {}).get('title', 'No Title')}"
                for item in news_items[:5]
            ]
            return "\n".join(lines)
        
        return await asyncio.to_thread(_get_news)
    except Exception as e:
        logger.error(f"[{symbol}] 新聞獲取失敗: {e}")
        return "無法獲取近期新聞。"
