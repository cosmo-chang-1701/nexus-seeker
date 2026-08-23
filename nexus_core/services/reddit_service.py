import asyncio
import random
import httpx
import logging
from typing import Optional

import config

logger = logging.getLogger(__name__)

# 限制同時透過 Cloudflare Tunnel 打向 edge scraper 的 Reddit 爬取請求數，
# 避免多個標的同時掃描時讓 edge 端過載或 Tunnel 逾時。
_reddit_sem = asyncio.Semaphore(2)


async def get_reddit_details(
    symbol: str, limit: int = 3, *, enable_tunnel: bool = True
) -> tuple[Optional[str], list[dict[str, str]]]:
    """透過 Cloudflare Tunnel 爬取 Reddit，同時回傳情緒摘要文字與結構化貼文清單 (含 URL)。

    Returns:
        (reddit_text, posts_list)
    """
    empty_posts: list[dict[str, str]] = []
    # ── Gate 1: 呼叫端明確關閉 ──────────────────────────────────
    if not enable_tunnel:
        logger.info(f"⏭️ [{symbol}] 呼叫端明確跳過本地 Tunnel (Reddit Scraper) 呼叫。")
        return None, empty_posts

    # ── Gate 2: TUNNEL_URL 配置檢查 ─────────────────────────────
    if not getattr(config, "TUNNEL_URL", ""):
        return "尚未配置本地 Tunnel URL，暫不抓取 Reddit 情緒。", empty_posts

    try:
        logger.info(
            f"[{symbol}] 啟動邊緣運算呼叫，透過 Tunnel 要求本地端爬取 Reddit..."
        )

        base_url = config.TUNNEL_URL.rstrip("/")

        # 透過 StockAliasMatrix 取得別名與構建精準 Boolean Search Query
        import urllib.parse
        from market_analysis.stock_alias_matrix import StockAliasMatrix

        aliases = await StockAliasMatrix.get_aliases_for_symbol(symbol)
        custom_query = StockAliasMatrix.build_reddit_query(symbol, aliases)
        query_encoded = urllib.parse.quote(custom_query)

        # 45 秒超時，給予本地端 429 重試/退避足夠的時間預算
        async with _reddit_sem:
            # 併發呼叫（如 asyncio.gather 批次掃描）時加入隨機抖動，自然錯開對
            # edge 端與 Reddit 的請求節奏，避免固定節奏被限流特徵辨識。
            await asyncio.sleep(random.uniform(0.5, 1.5))

            async with httpx.AsyncClient(timeout=45.0, follow_redirects=True) as client:
                req_url = f"{base_url}/api/v1/scrape/reddit/{symbol}?limit={limit}&custom_query={query_encoded}"

                res = await client.get(req_url)
                res.raise_for_status()

                # 解析本地端回傳的 JSON
                response_json = res.json()
                if response_json.get("status") == "success":
                    logger.info(f"[{symbol}] 成功從本地端取得 Reddit 資料！")
                    raw_data = response_json.get("data")
                    posts = response_json.get("posts") or []
                    return raw_data, posts
                else:
                    logger.warning(
                        f"[{symbol}] 本地端爬取發生內部錯誤: {response_json.get('data')}"
                    )
                    return "本地備援節點發生錯誤，暫無情緒資料。", empty_posts

    except httpx.ReadTimeout:
        logger.warning(f"[{symbol}] Tunnel 請求超時，本地端無回應。")
        return "本地節點連線超時。", empty_posts
    except Exception as e:
        logger.warning(f"[{symbol}] 呼叫本地 Tunnel 失敗: {e}")
        return "邊緣運算節點連線異常。", empty_posts


async def get_reddit_context(
    symbol: str, limit: int = 3, *, enable_tunnel: bool = True
) -> Optional[str]:
    """透過 Cloudflare Tunnel 呼叫本地端爬取 Reddit。

    Returns:
        Reddit 情緒摘要文字，或 ``None`` 表示已跳過呼叫。
    """
    text, _ = await get_reddit_details(symbol, limit=limit, enable_tunnel=enable_tunnel)
    return text


async def get_reddit_context_batch(
    symbols: list[str], limit_per_symbol: int = 5
) -> dict[str, Optional[str]]:
    """一次性抓取版塊最新貼文清單，並在本地端對多個標的做關鍵字比對。

    相較於逐標的呼叫 :func:`get_reddit_context`，本函式只對 Reddit 發出
    **1 次** HTTP 請求即可覆蓋整批 ``symbols``，大幅降低每日批次任務
    （如 watchlist 情緒快取更新）的請求數量與 429 風險。適合覆蓋大量標的
    的低頻批次場景；單一標的的精準查詢仍應使用 :func:`get_reddit_context`。

    Returns:
        ``{symbol: 情緒摘要文字或 None}``。若整批請求失敗或本地 Tunnel
        被關閉，所有標的一律回傳 ``None``（維持與 ``get_reddit_context``
        一致的降級語意）。
    """
    no_match_message = "過去 24 小時內無相關討論。"
    empty_result: dict[str, Optional[str]] = dict.fromkeys(symbols, None)

    if not symbols:
        return {}

    # ── Gate: TUNNEL_URL 配置檢查 ─────────────────────────────
    if not getattr(config, "TUNNEL_URL", ""):
        return empty_result

    try:
        logger.info(
            f"[批次] 啟動邊緣運算呼叫，一次抓取 Reddit 最新貼文清單（{len(symbols)} 個標的）..."
        )

        base_url = config.TUNNEL_URL.rstrip("/")

        async with _reddit_sem:
            await asyncio.sleep(random.uniform(0.5, 1.5))

            async with httpx.AsyncClient(timeout=45.0, follow_redirects=True) as client:
                req_url = f"{base_url}/api/v1/scrape/reddit/feed?limit=100"
                res = await client.get(req_url)
                res.raise_for_status()

                response_json = res.json()
                if response_json.get("status") != "success":
                    logger.warning(
                        f"[批次] 本地端爬取發生內部錯誤: {response_json.get('data')}"
                    )
                    return empty_result

                posts: list[dict[str, str]] = response_json.get("data") or []

        from market_analysis.stock_alias_matrix import StockAliasMatrix

        results: dict[str, Optional[str]] = {}
        for symbol in symbols:
            aliases = await StockAliasMatrix.get_aliases_for_symbol(symbol)
            matched = [
                post
                for post in posts
                if StockAliasMatrix.is_text_matching_symbol(
                    post.get("title", ""), symbol, aliases
                )
            ][:limit_per_symbol]

            if not matched:
                results[symbol] = no_match_message
                continue

            posts_text = "".join(
                f"[{post.get('subreddit', 'unknown')}] {post.get('title', 'N/A')}\n"
                for post in matched
            )
            results[symbol] = posts_text

        logger.info(
            f"[批次] 成功從本地端取得 Reddit 資料，已對 {len(symbols)} 個標的完成本地關鍵字比對。"
        )
        return results

    except httpx.ReadTimeout:
        logger.warning("[批次] Tunnel 請求超時，本地端無回應。")
        return empty_result
    except Exception as e:
        logger.warning(f"[批次] 呼叫本地 Tunnel 失敗: {e}")
        return empty_result
