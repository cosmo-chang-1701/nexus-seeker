from datetime import datetime, timezone
import logging
import time
from typing import Any, Dict, List, Optional
from services import market_data_service

logger = logging.getLogger(__name__)


def _format_relative_time(timestamp: Optional[int | float]) -> str:
    """將 Unix timestamp 格式化為友善的相對時間字串。"""
    if not timestamp or timestamp <= 0:
        return ""
    try:
        now_ts = time.time()
        diff = max(0.0, now_ts - float(timestamp))
        if diff < 60:
            return "剛剛"
        elif diff < 3600:
            return f"{int(diff // 60)}分鐘前"
        elif diff < 86400:
            return f"{int(diff // 3600)}小時前"
        elif diff < 86400 * 7:
            return f"{int(diff // 86400)}天前"
        else:
            dt = datetime.fromtimestamp(timestamp, tz=timezone.utc)
            return dt.strftime("%m/%d")
    except Exception:
        return ""


async def fetch_recent_news_structured(
    symbol: str, limit: int = 5
) -> List[Dict[str, Any]]:
    """非同步獲取標的近期結構化新聞 (包含 URL、來源機構、發布時間)。"""
    try:
        raw_items = await market_data_service.get_company_news(symbol, limit=limit)
        if not raw_items:
            return []

        structured_news: List[Dict[str, Any]] = []
        for item in raw_items:
            if not isinstance(item, dict):
                continue
            headline = str(item.get("headline", "")).strip()
            if not headline:
                continue
            url = str(item.get("url", "")).strip()
            source = str(item.get("source", "")).strip()
            dt_raw = item.get("datetime")
            time_tag = _format_relative_time(dt_raw) if dt_raw else ""

            structured_news.append(
                {
                    "headline": headline,
                    "url": url,
                    "source": source,
                    "datetime": dt_raw,
                    "time_tag": time_tag,
                    "summary": str(item.get("summary", "")).strip(),
                }
            )
        return structured_news
    except Exception as e:
        logger.error(f"[{symbol}] 獲取結構化新聞失敗: {e}")
        return []


async def fetch_recent_news(symbol: str, limit: int = 5) -> str:
    """非同步獲取標的近期的新聞標題 (透過 Finnhub)"""
    try:
        news_items = await market_data_service.get_company_news(symbol, limit=limit)
        if not news_items:
            return "近期無重大新聞。"

        lines = [f"▪️ {item.get('headline', 'No Title')}" for item in news_items]
        return "\n".join(lines)
    except Exception as e:
        logger.error(f"[{symbol}] 新聞獲取失敗: {e}")
        return "無法獲取近期新聞。"
