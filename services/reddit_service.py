import httpx
import logging

logger = logging.getLogger(__name__)

async def get_reddit_context(symbol: str) -> str:
    """Reddit 公開 JSON 爬蟲 (低頻次適用)"""
    subreddits = "wallstreetbets+stocks+options"
    query = f"{symbol} OR ${symbol}"
    
    url = f"https://www.reddit.com/r/{subreddits}/search.json"
    
    params = {
        "q": query,
        "sort": "top",
        "t": "day",
        "limit": 10,
        "restrict_sr": "on"
    }
    
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/121.0.0.0 Safari/537.36"
    }

    try:
        async with httpx.AsyncClient() as client:
            # 加入 timeout 避免網路卡住
            res = await client.get(url, params=params, headers=headers, timeout=10.0)
            res.raise_for_status()
            data = res.json()

            posts_text = ""
            for child in data.get("data", {}).get("children", []):
                post = child["data"]
                posts_text += f"[{post['subreddit']} | 共識分數:{post['score']}] {post['title']}\n"

            return posts_text if posts_text else "Reddit 無相關討論。"
            
    except httpx.HTTPStatusError as e:
        logger.error(f"[{symbol}] Reddit 伺服器阻擋存取 (HTTP {e.response.status_code}): {e}")
        return "Reddit 伺服器阻擋存取，暫無情緒資料。"
    except Exception as e:
        logger.error(f"[{symbol}] Reddit 獲取失敗: {e}")
        return "無法獲取 Reddit 情緒。"