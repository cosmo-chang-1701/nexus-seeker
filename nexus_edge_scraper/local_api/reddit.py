"""local_api：Reddit RSS 抓取（版塊最新貼文清單 / 個股關鍵字搜尋）。"""

from typing import Any
import asyncio
import logging
import os
import random
import time

import httpx
from fastapi import APIRouter, Query

logger = logging.getLogger(__name__)

router = APIRouter()

_REDDIT_CACHE_TTL = 600  # 10 分鐘，避免短時間內對同一標的重複打 Reddit RSS 觸發 429
_reddit_cache: dict[str, tuple[dict[str, Any], float]] = {}

_REDDIT_UA_PLACEHOLDER = (
    "script:nexus-seeker-sentiment-tracker:v1.0 "
    "(by /u/CHANGE_ME_SET_REDDIT_USER_AGENT_ENV)"
)
REDDIT_USER_AGENT = os.getenv("REDDIT_USER_AGENT", _REDDIT_UA_PLACEHOLDER)
if REDDIT_USER_AGENT == _REDDIT_UA_PLACEHOLDER:
    logger.warning(
        "⚠️ REDDIT_USER_AGENT 尚未設定，正使用預設佔位字串，"
        "Reddit 可能因未使用自訂/獨特 UA 而優先限流。"
        "請在部署環境設定 REDDIT_USER_AGENT（格式：<platform>:<app_id>:<version> (by /u/<username>)）。"
    )


async def _fetch_reddit_rss(
    client: httpx.AsyncClient, url: str, max_retries: int = 3
) -> httpx.Response:
    """帶指數退避與抖動（jitter）的 Reddit RSS 請求，統一處理 429 限流。"""
    headers = {"User-Agent": REDDIT_USER_AGENT}
    base_delay = 5.0

    for attempt in range(max_retries + 1):
        await asyncio.sleep(random.uniform(0.3, 1.0))
        resp = await client.get(url, headers=headers)

        if resp.status_code != 429:
            return resp

        if attempt >= max_retries:
            return resp

        retry_after = resp.headers.get("Retry-After")
        if retry_after is not None and retry_after.isdigit():
            delay = float(retry_after)
        else:
            delay = base_delay * (2**attempt) + random.uniform(1.0, 3.0)
        delay = min(delay, 30.0)

        logger.warning(
            f"Reddit RSS 429 Too Many Requests（第 {attempt + 1} 次），{delay:.1f}s 後重試"
        )
        await asyncio.sleep(delay)

    return resp


@router.get("/api/v1/scrape/reddit/feed")
async def scrape_reddit_feed(
    limit: int = Query(100, description="抓取貼文數量上限"),
) -> dict[str, Any]:
    """一次性抓取版塊最新貼文清單（不帶關鍵字），交由上游本地端做多標的關鍵字比對。

    相較於逐標的呼叫 /api/v1/scrape/reddit/{symbol}，這個端點讓批次任務
    （例如每日 watchlist 情緒快取更新）只需對 Reddit 發出 1 次請求，
    大幅降低請求數量與 429 風險。

    註：此路由必須註冊在 /api/v1/scrape/reddit/{symbol} 之前，
    否則 "feed" 會被動態路由當成 symbol 攔截。
    """
    import xml.etree.ElementTree as ET

    cache_key = f"__feed__|{limit}"
    cached = _reddit_cache.get(cache_key)
    if cached is not None:
        data, expiry = cached
        if time.time() < expiry:
            return data

    url = (
        f"https://www.reddit.com/r/wallstreetbets+stocks+options/new.rss?limit={limit}"
    )

    try:
        async with httpx.AsyncClient(timeout=15.0, follow_redirects=True) as client:
            resp = await _fetch_reddit_rss(client, url)
            resp.raise_for_status()

            root = ET.fromstring(resp.text)
            ns = {"atom": "http://www.w3.org/2005/Atom"}
            entries = root.findall("atom:entry", ns)

            posts: list[dict[str, str]] = []
            for entry in entries[:limit]:
                title = entry.find("atom:title", ns)
                title_text = title.text if title is not None else "N/A"

                category = entry.find("atom:category", ns)
                sub = (
                    category.attrib.get("label", "unknown")
                    if category is not None
                    else "unknown"
                )
                sub = sub.replace("r/", "")

                published = entry.find("atom:published", ns)
                published_text = published.text if published is not None else ""

                link = entry.find("atom:link", ns)
                link_url = link.attrib.get("href", "") if link is not None else ""

                posts.append(
                    {
                        "title": title_text or "N/A",
                        "subreddit": sub,
                        "published": published_text or "",
                        "url": link_url,
                    }
                )

            result = {"status": "success", "data": posts}
            _reddit_cache[cache_key] = (result, time.time() + _REDDIT_CACHE_TTL)
            return result

    except Exception as e:
        logger.error(f"Reddit RSS feed 執行嚴重例外: {str(e)}")
        return {"status": "error", "data": f"本地端執行例外: {str(e)}"}


@router.get("/api/v1/scrape/reddit/{symbol}")
async def scrape_reddit(
    symbol: str,
    company_name: str = Query("", description="公司名稱"),
    custom_query: str = Query("", description="自訂搜尋條件"),
    limit: int = Query(5, description="回傳的貼文數量上限"),
) -> dict[str, Any]:
    import xml.etree.ElementTree as ET
    import urllib.parse

    symbol_clean = symbol.replace("$", "")

    if custom_query:
        q_term = custom_query
    elif company_name:
        q_term = f'"{symbol_clean}" OR "{company_name}"'
    else:
        q_term = f'"{symbol_clean}"'

    q_encoded = urllib.parse.quote(q_term)

    cache_key = f"{q_term}|{limit}"
    cached = _reddit_cache.get(cache_key)
    if cached is not None:
        data, expiry = cached
        if time.time() < expiry:
            return data

    url = (
        f"https://www.reddit.com/r/wallstreetbets+stocks+options/search.rss"
        f"?q={q_encoded}"
        f"&restrict_sr=on"
        f"&sort=new"
        f"&t=day"
    )

    try:
        async with httpx.AsyncClient(timeout=15.0, follow_redirects=True) as client:
            resp = await _fetch_reddit_rss(client, url)
            resp.raise_for_status()

            root = ET.fromstring(resp.text)

            # XML namespace for Atom
            ns = {"atom": "http://www.w3.org/2005/Atom"}
            entries = root.findall("atom:entry", ns)

            if not entries:
                no_posts: list[dict[str, str]] = []
                empty_result: dict[str, Any] = {
                    "status": "success",
                    "data": "過去 24 小時內無相關討論。",
                    "posts": no_posts,
                }
                _reddit_cache[cache_key] = (
                    empty_result,
                    time.time() + _REDDIT_CACHE_TTL,
                )
                return empty_result

            posts_text = ""
            posts_list: list[dict[str, str]] = []
            for entry in entries[:limit]:
                title = entry.find("atom:title", ns)
                title_text: str = (
                    title.text
                    if title is not None and title.text is not None
                    else "N/A"
                )

                category = entry.find("atom:category", ns)
                sub: str = (
                    category.attrib.get("label", "unknown")
                    if category is not None
                    else "unknown"
                )
                sub = sub.replace("r/", "")

                link = entry.find("atom:link", ns)
                link_url: str = link.attrib.get("href", "") if link is not None else ""

                published = entry.find("atom:published", ns)
                published_text: str = (
                    published.text
                    if published is not None and published.text is not None
                    else ""
                )

                posts_text += f"[{sub}] {title_text}\n"
                posts_list.append(
                    {
                        "title": title_text,
                        "subreddit": sub,
                        "url": link_url,
                        "published": published_text,
                    }
                )

            result = {
                "status": "success",
                "data": posts_text,
                "posts": posts_list,
            }
            _reddit_cache[cache_key] = (result, time.time() + _REDDIT_CACHE_TTL)
            return result

    except Exception as e:
        logger.error(f"Reddit RSS 執行嚴重例外: {str(e)}")
        return {"status": "error", "data": f"本地端執行例外: {str(e)}"}
