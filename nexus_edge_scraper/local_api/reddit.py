"""local_api：Reddit RSS 抓取（版塊最新貼文清單 / 個股關鍵字搜尋）。"""

import asyncio
import logging
import os
import random
import re
import time
import urllib.parse
import xml.etree.ElementTree as ET
from typing import Any

import httpx
from fastapi import APIRouter, Query

logger = logging.getLogger(__name__)

router = APIRouter()

_REDDIT_CACHE_TTL = 600  # 10 分鐘，避免短時間內對同一標的重複打 Reddit RSS 觸發 429
_reddit_cache: dict[str, tuple[dict[str, Any], float]] = {}

_TARGET_SUBREDDITS: tuple[str, ...] = ("wallstreetbets", "stocks", "options")
_ATOM_NS: dict[str, str] = {"atom": "http://www.w3.org/2005/Atom"}
_NO_POSTS_MESSAGE = "過去 24 小時內無相關討論。"
_FEED_CACHE_LIMIT = 100
_TOTAL_FETCH_BUDGET_SEC = 25.0
_REQUEST_TIMEOUT_SEC = 8.0

_DEFAULT_REDDIT_USER_AGENT = "linux:nexus-seeker-edge:v1.19 (by /u/nexus_seeker_bot)"
REDDIT_USER_AGENT = (
    os.getenv("REDDIT_USER_AGENT") or ""
).strip() or _DEFAULT_REDDIT_USER_AGENT
if REDDIT_USER_AGENT == _DEFAULT_REDDIT_USER_AGENT:
    logger.warning(
        "⚠️ REDDIT_USER_AGENT 尚未設定，正使用預設應用程式 User-Agent，"
        "Reddit 可能因未使用專屬 UA 而優先限流。"
        "請在部署環境設定 REDDIT_USER_AGENT（格式：<platform>:<app_id>:<version> (by /u/<username>)）。"
    )


def _now_monotonic() -> float:
    """回傳單調時鐘秒數（獨立包裝以便測試隔離，不影響 asyncio 內部時鐘）。"""
    return time.monotonic()


def _classify_http_status(status_code: int) -> str:
    """將上游 HTTP 狀態碼對應至結構化錯誤代碼。"""
    if status_code == 429:
        return "rate_limited"
    if status_code == 404:
        return "upstream_404"
    if status_code == 403:
        return "upstream_403"
    return "upstream_error"


def _parse_atom_entries(xml_text: str, limit: int) -> list[dict[str, str]]:
    """解析 Reddit Atom XML 回應為標準化貼文清單。

    若 XML 格式損毀或非合法 Atom 文件，會拋出 ``ET.ParseError`` 供呼叫端降級處理。
    """
    root = ET.fromstring(xml_text)
    if root.tag != "{http://www.w3.org/2005/Atom}feed":
        raise ET.ParseError(f"非預期 Atom 根節點: {root.tag}")
    if limit <= 0:
        return []
    entries = root.findall("atom:entry", _ATOM_NS)

    posts: list[dict[str, str]] = []
    for entry in entries[:limit]:
        title = entry.find("atom:title", _ATOM_NS)
        title_text = (
            title.text if title is not None and title.text is not None else "N/A"
        )

        category = entry.find("atom:category", _ATOM_NS)
        sub = (
            category.attrib.get("label", "unknown")
            if category is not None
            else "unknown"
        )
        sub = sub.replace("r/", "")

        published = entry.find("atom:published", _ATOM_NS)
        published_text = (
            published.text
            if published is not None and published.text is not None
            else ""
        )

        link = entry.find("atom:link", _ATOM_NS)
        link_url = link.attrib.get("href", "") if link is not None else ""

        posts.append(
            {
                "title": title_text or "N/A",
                "subreddit": sub,
                "published": published_text,
                "url": link_url,
            }
        )
    return posts


def _extract_query_tokens(
    symbol_clean: str, company_name: str, custom_query: str
) -> list[str]:
    """從代碼、公司名稱與布林查詢字串萃取出待比對的關鍵字清單。"""
    raw_tokens: list[str] = []
    if symbol_clean.strip():
        raw_tokens.append(symbol_clean.strip())
    if company_name.strip():
        raw_tokens.append(company_name.strip())

    if custom_query.strip():
        quoted = re.findall(r'"([^"]+)"', custom_query)
        raw_tokens.extend(quoted)

        unquoted_text = re.sub(r'"[^"]*"', " ", custom_query)
        unquoted_text = unquoted_text.replace("(", " ").replace(")", " ")
        for piece in unquoted_text.split():
            if piece.upper() in {"OR", "AND", "NOT"}:
                continue
            raw_tokens.append(piece)

    tokens: list[str] = []
    seen: set[str] = set()
    for raw in raw_tokens:
        cleaned = raw.strip().lstrip("$").strip()
        if not cleaned:
            continue
        key = cleaned.lower()
        if key not in seen:
            seen.add(key)
            tokens.append(cleaned)
    return tokens


def _entry_matches_query(
    title: str, symbol_clean: str, company_name: str, custom_query: str
) -> bool:
    """以詞界正規表示式（不分大小寫）比對貼文標題是否符合目標標的查詢。"""
    if not title or title == "N/A":
        return False

    tokens = _extract_query_tokens(symbol_clean, company_name, custom_query)
    for token in tokens:
        pattern = rf"(?<!\w){re.escape(token)}(?!\w)"
        if re.search(pattern, title, re.IGNORECASE):
            return True
    return False


def _format_symbol_response(
    posts_list: list[dict[str, str]],
) -> dict[str, Any]:
    """組裝個股 Reddit 查詢的成功回應格式。"""
    if not posts_list:
        empty_posts: list[dict[str, str]] = []
        return {
            "status": "success",
            "data": _NO_POSTS_MESSAGE,
            "posts": empty_posts,
        }

    posts_text = "".join(
        f"[{post['subreddit']}] {post['title']}\n" for post in posts_list
    )
    return {
        "status": "success",
        "data": posts_text,
        "posts": posts_list,
    }


def _deduplicate_posts(posts: list[dict[str, str]], limit: int) -> list[dict[str, str]]:
    """依 URL 與標題去除重複貼文，並依發佈時間降冪排序後截斷至上限。"""
    seen_urls: set[str] = set()
    seen_titles: set[str] = set()
    unique_posts: list[dict[str, str]] = []

    for post in posts:
        url = post.get("url", "").strip()
        title = post.get("title", "").strip()
        title_key = title.lower() if title and title != "N/A" else ""

        if url and url in seen_urls:
            continue
        if title_key and title_key in seen_titles:
            continue

        if url:
            seen_urls.add(url)
        if title_key:
            seen_titles.add(title_key)
        unique_posts.append(post)

    unique_posts.sort(key=lambda item: item.get("published", ""), reverse=True)
    return unique_posts[: max(limit, 0)]


def _get_cached_feed_entries() -> list[dict[str, str]] | None:
    """從本地快取讀取未過期的最新貼文清單，供個股搜尋最後一線降級使用。"""
    now = time.time()
    preferred_key = f"__feed__|{_FEED_CACHE_LIMIT}"
    candidate_keys = [preferred_key] + [
        k for k in _reddit_cache if k.startswith("__feed__|") and k != preferred_key
    ]
    for key in candidate_keys:
        cached = _reddit_cache.get(key)
        if cached is None:
            continue
        data, expiry = cached
        if now >= expiry or data.get("status") != "success":
            continue
        raw_posts = data.get("data")
        if isinstance(raw_posts, list):
            return raw_posts
    return None


async def _fetch_reddit_rss(
    client: httpx.AsyncClient,
    url: str,
    max_retries: int = 3,
    deadline: float | None = None,
) -> httpx.Response:
    """帶指數退避與抖動（jitter）的 Reddit RSS 請求，統一處理 429 限流與總預算上限。"""
    headers = {"User-Agent": REDDIT_USER_AGENT}
    base_delay = 5.0
    last_resp: httpx.Response | None = None

    for attempt in range(max_retries + 1):
        jitter = random.uniform(0.3, 1.0)
        if deadline is not None:
            remaining = deadline - _now_monotonic()
            if remaining <= 0:
                if last_resp is not None:
                    return last_resp
            else:
                await asyncio.sleep(min(jitter, remaining))
                if last_resp is not None and _now_monotonic() >= deadline:
                    return last_resp
        else:
            await asyncio.sleep(jitter)

        resp = await client.get(url, headers=headers)
        last_resp = resp

        if resp.status_code != 429:
            return resp

        if attempt >= max_retries:
            return resp

        retry_after = resp.headers.get("Retry-After")
        if retry_after is not None and retry_after.isdigit():
            delay = float(retry_after)
        else:
            delay = base_delay * (2**attempt) + random.uniform(1.0, 3.0)
        delay = min(delay, 15.0)

        if deadline is not None and _now_monotonic() + delay >= deadline:
            logger.warning(
                f"Reddit RSS 429 Too Many Requests（第 {attempt + 1} 次），"
                f"退避 {delay:.1f}s 將超過總預算，中止重試"
            )
            return resp

        logger.warning(
            f"Reddit RSS 429 Too Many Requests（第 {attempt + 1} 次），{delay:.1f}s 後重試"
        )
        await asyncio.sleep(delay)

    assert last_resp is not None
    return last_resp


async def _fetch_and_parse_url(
    client: httpx.AsyncClient,
    url: str,
    limit: int,
    max_retries: int = 3,
    deadline: float | None = None,
) -> tuple[list[dict[str, str]] | None, str | None, str | None]:
    """抓取並解析單一 Atom RSS URL，回傳 ``(entries, error_code, error_detail)``。"""
    try:
        resp = await _fetch_reddit_rss(
            client, url, max_retries=max_retries, deadline=deadline
        )
        if resp.status_code >= 400:
            code = _classify_http_status(resp.status_code)
            return None, code, f"HTTP {resp.status_code} ({url})"
        entries = _parse_atom_entries(resp.text, limit=limit)
        return entries, None, None
    except httpx.TimeoutException as e:
        return None, "timeout", f"連線逾時: {e}"
    except httpx.HTTPStatusError as e:
        code = _classify_http_status(e.response.status_code)
        return None, code, f"HTTP {e.response.status_code}: {e}"
    except ET.ParseError as e:
        return None, "upstream_error", f"Atom XML 解析失敗: {e}"
    except Exception as e:
        return None, "upstream_error", str(e)


async def _fetch_feed_entries(
    client: httpx.AsyncClient,
    limit: int,
    deadline: float | None = None,
) -> tuple[list[dict[str, str]] | None, str | None, str | None]:
    """抓取版塊最新貼文清單，Multireddit 失敗時自動降級為逐版塊抓取並去重。"""
    multi_sub = "+".join(_TARGET_SUBREDDITS)
    multireddit_url = f"https://www.reddit.com/r/{multi_sub}/new.rss?limit={limit}"

    entries, err_code, err_detail = await _fetch_and_parse_url(
        client, multireddit_url, limit=limit, max_retries=2, deadline=deadline
    )
    if entries is not None:
        return entries, None, None

    logger.warning(
        f"Multireddit new.rss 抓取失敗（{err_code}: {err_detail}），降級為逐版塊抓取..."
    )

    merged_posts: list[dict[str, str]] = []
    any_succeeded = False
    last_code = err_code
    last_detail = err_detail

    for sub in _TARGET_SUBREDDITS:
        if deadline is not None and _now_monotonic() >= deadline:
            logger.warning("已達 Reddit RSS 總牆鐘時間上限，停止後續版塊降級請求。")
            break

        sub_url = f"https://www.reddit.com/r/{sub}/new.rss?limit={limit}"
        sub_entries, sub_code, sub_detail = await _fetch_and_parse_url(
            client, sub_url, limit=limit, max_retries=1, deadline=deadline
        )
        if sub_entries is not None:
            any_succeeded = True
            merged_posts.extend(sub_entries)
        else:
            last_code = sub_code
            last_detail = sub_detail

    if any_succeeded:
        return _deduplicate_posts(merged_posts, limit=limit), None, None

    return None, last_code or "upstream_error", last_detail or "所有版塊抓取均失敗"


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
    cache_key = f"__feed__|{limit}"
    cached = _reddit_cache.get(cache_key)
    if cached is not None:
        data, expiry = cached
        if time.time() < expiry:
            return data

    deadline = _now_monotonic() + _TOTAL_FETCH_BUDGET_SEC
    try:
        async with httpx.AsyncClient(
            timeout=_REQUEST_TIMEOUT_SEC, follow_redirects=True
        ) as client:
            posts, err_code, err_detail = await _fetch_feed_entries(
                client, limit=limit, deadline=deadline
            )
            if posts is not None:
                result: dict[str, Any] = {"status": "success", "data": posts}
                _reddit_cache[cache_key] = (result, time.time() + _REDDIT_CACHE_TTL)
                return result

        final_code = err_code or "upstream_error"
        final_detail = err_detail or "未知錯誤"
        logger.error(f"Reddit RSS feed 執行失敗 [{final_code}]: {final_detail}")
        return {
            "status": "error",
            "error_code": final_code,
            "data": f"本地端執行例外: {final_detail}",
        }
    except Exception as e:
        logger.error(f"Reddit RSS feed 執行嚴重例外: {str(e)}")
        return {
            "status": "error",
            "error_code": "upstream_error",
            "data": f"本地端執行例外: {str(e)}",
        }


@router.get("/api/v1/scrape/reddit/{symbol}")
async def scrape_reddit(
    symbol: str,
    company_name: str = Query("", description="公司名稱"),
    custom_query: str = Query("", description="自訂搜尋條件"),
    limit: int = Query(5, description="回傳的貼文數量上限"),
) -> dict[str, Any]:
    symbol_clean = symbol.replace("$", "").strip()

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

    multi_sub = "+".join(_TARGET_SUBREDDITS)
    primary_url = (
        f"https://www.reddit.com/r/{multi_sub}/search.rss"
        f"?q={q_encoded}"
        f"&restrict_sr=on"
        f"&sort=new"
        f"&t=day"
    )
    global_search_url = (
        f"https://www.reddit.com/search.rss?q={q_encoded}&sort=new&t=day"
    )

    deadline = _now_monotonic() + _TOTAL_FETCH_BUDGET_SEC
    last_code: str | None = None
    last_detail: str | None = None

    try:
        async with httpx.AsyncClient(
            timeout=_REQUEST_TIMEOUT_SEC, follow_redirects=True
        ) as client:
            # 第一層：Multireddit search.rss
            entries, err_code, err_detail = await _fetch_and_parse_url(
                client, primary_url, limit=limit, max_retries=2, deadline=deadline
            )
            if entries is not None:
                result = _format_symbol_response(entries)
                _reddit_cache[cache_key] = (result, time.time() + _REDDIT_CACHE_TTL)
                return result

            last_code, last_detail = err_code, err_detail
            logger.warning(
                f"[{symbol_clean}] 主搜尋 Multireddit search.rss 失敗"
                f"（{err_code}: {err_detail}），切換至全站 search.rss 備援..."
            )

            # 第二層：全站 search.rss
            if _now_monotonic() < deadline:
                global_entries, g_code, g_detail = await _fetch_and_parse_url(
                    client,
                    global_search_url,
                    limit=limit,
                    max_retries=1,
                    deadline=deadline,
                )
                if global_entries is not None:
                    result = _format_symbol_response(global_entries)
                    _reddit_cache[cache_key] = (result, time.time() + _REDDIT_CACHE_TTL)
                    return result

                last_code, last_detail = g_code, g_detail
                logger.warning(
                    f"[{symbol_clean}] 全站 search.rss 失敗"
                    f"（{g_code}: {g_detail}），降級為最新貼文清單本地比對..."
                )

            # 第三層：使用快取或最新抓取之 new.rss 清單並做本地關鍵字過濾
            feed_entries = _get_cached_feed_entries()
            if feed_entries is None and _now_monotonic() < deadline:
                fetched_feed, f_code, f_detail = await _fetch_feed_entries(
                    client, limit=_FEED_CACHE_LIMIT, deadline=deadline
                )
                if fetched_feed is not None:
                    feed_entries = fetched_feed
                    feed_cache_payload: dict[str, Any] = {
                        "status": "success",
                        "data": fetched_feed,
                    }
                    _reddit_cache[f"__feed__|{_FEED_CACHE_LIMIT}"] = (
                        feed_cache_payload,
                        time.time() + _REDDIT_CACHE_TTL,
                    )
                else:
                    last_code, last_detail = f_code, f_detail

            if feed_entries is not None:
                matched = [
                    post
                    for post in feed_entries
                    if _entry_matches_query(
                        post.get("title", ""),
                        symbol_clean,
                        company_name,
                        custom_query,
                    )
                ][: max(limit, 0)]
                result = _format_symbol_response(matched)
                _reddit_cache[cache_key] = (result, time.time() + _REDDIT_CACHE_TTL)
                return result

        final_code = last_code or "upstream_error"
        final_detail = last_detail or "未知錯誤"
        logger.error(
            f"[{symbol_clean}] Reddit RSS 執行失敗 [{final_code}]: {final_detail}"
        )
        return {
            "status": "error",
            "error_code": final_code,
            "data": f"本地端執行例外: {final_detail}",
        }

    except Exception as e:
        logger.error(f"Reddit RSS 執行嚴重例外: {str(e)}")
        return {
            "status": "error",
            "error_code": "upstream_error",
            "data": f"本地端執行例外: {str(e)}",
        }
