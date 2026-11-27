import asyncio
import logging
import random
import re
from typing import Any, Mapping, Optional, Sequence

import httpx

import config
from database.cache import get_kv_cache_fresh
from services.edge_auth import edge_client_kwargs

logger = logging.getLogger(__name__)

# 限制同時透過 Cloudflare Tunnel 打向 edge scraper 的 Reddit 爬取請求數，
# 避免多個標的同時掃描時讓 edge 端過載或 Tunnel 逾時。
_reddit_sem = asyncio.Semaphore(2)

# 08:30 ET 每日批次快取的可用寬限上限（涵蓋交易日與隔夜）。
REDDIT_CACHE_FALLBACK_MAX_AGE_SECONDS: float = 36 * 3600.0
REDDIT_NO_MATCH_MESSAGE: str = "過去 24 小時內無相關討論。"
REDDIT_CACHE_MARKER: str = "[快取]"

# 具名錯誤哨兵字串：用於區分失敗原因，並確保 is_reddit_error_text 僅匹配服務錯誤而非貼文標題。
REDDIT_ERR_TUNNEL_UNSET: str = "尚未配置本地 Tunnel URL，暫不抓取 Reddit 情緒。"
REDDIT_ERR_TIMEOUT: str = "本地節點連線超時。"
REDDIT_ERR_RATE_LIMITED: str = "Reddit 上游限流 (429)，暫無情緒資料。"
REDDIT_ERR_UPSTREAM_HTTP: str = "Reddit 上游端點異常 (404/403)，暫無情緒資料。"
REDDIT_ERR_EDGE_GENERIC: str = "本地備援節點發生錯誤，暫無情緒資料。"
REDDIT_ERR_CONNECTION: str = "邊緣運算節點連線異常。"

_ERROR_SENTINELS: tuple[str, ...] = (
    REDDIT_ERR_TUNNEL_UNSET,
    REDDIT_ERR_TIMEOUT,
    REDDIT_ERR_RATE_LIMITED,
    REDDIT_ERR_UPSTREAM_HTTP,
    REDDIT_ERR_EDGE_GENERIC,
    REDDIT_ERR_CONNECTION,
    "本地端執行例外:",
)

_CACHED_POST_RE = re.compile(r"^\[([^\]]+)\]\s+(.+)$")
_EN_WORD_RE = re.compile(r"[a-zA-Z']+")

_EN_NEGATORS: frozenset[str] = frozenset(
    {
        "not",
        "no",
        "never",
        "don't",
        "dont",
        "didn't",
        "didnt",
        "isn't",
        "isnt",
        "aren't",
        "arent",
        "won't",
        "wont",
        "can't",
        "cant",
        "hardly",
    }
)

_EN_BULLISH_TERMS: frozenset[str] = frozenset(
    {
        "bull",
        "bullish",
        "calls",
        "call",
        "moon",
        "mooning",
        "rocket",
        "yolo",
        "buy",
        "buying",
        "long",
        "beat",
        "beats",
        "soar",
        "soars",
        "soaring",
        "surge",
        "surges",
        "breakout",
        "rally",
        "upgrade",
        "upgraded",
        "ath",
        "accumulate",
        "undervalued",
        "squeeze",
        "rip",
        "ripping",
        "tendies",
        "pump",
        "pumping",
        "oversold",
    }
)

_EN_BEARISH_TERMS: frozenset[str] = frozenset(
    {
        "bear",
        "bearish",
        "puts",
        "put",
        "crash",
        "crashing",
        "dump",
        "dumping",
        "sell",
        "selling",
        "short",
        "shorting",
        "miss",
        "misses",
        "missed",
        "plunge",
        "plunges",
        "tank",
        "tanking",
        "drill",
        "drilling",
        "downgrade",
        "overvalued",
        "bubble",
        "wrecked",
        "shambles",
        "destroyed",
        "collapse",
        "rug",
        "bagholder",
        "drop",
        "drops",
        "selloff",
        "fall",
        "falling",
    }
)

_ZH_BULLISH_TERMS: tuple[str, ...] = (
    "看多",
    "樂觀",
    "買進",
    "加碼",
    "突破",
    "暴漲",
    "大漲",
    "起飛",
    "超預期",
    "做多",
)

_ZH_BEARISH_TERMS: tuple[str, ...] = (
    "看空",
    "悲觀",
    "恐慌",
    "崩盤",
    "暴跌",
    "大跌",
    "做空",
    "放空",
    "減碼",
    "出貨",
    "破底",
    "泡沫",
)

_ZH_NEGATORS: tuple[str, ...] = ("不要", "沒有", "不", "沒", "別", "非", "未")


def is_reddit_error_text(text: Optional[str]) -> bool:
    """僅比對已知的 Reddit 服務錯誤哨兵字串（精確／前綴比對），不誤傷貼文標題。"""
    if not text:
        return False
    cleaned = text.strip()
    if not cleaned:
        return False
    return any(
        cleaned == sentinel or cleaned.startswith(sentinel)
        for sentinel in _ERROR_SENTINELS
    )


def _parse_cached_reddit_posts(cached_text: str) -> list[dict[str, str]]:
    """將 kv_cache 中的 `[subreddit] title` 多行文字解析為結構化貼文清單。"""
    posts: list[dict[str, str]] = []
    for raw_line in cached_text.splitlines():
        line = raw_line.strip()
        if not line or line == REDDIT_CACHE_MARKER or line == REDDIT_NO_MATCH_MESSAGE:
            continue
        match = _CACHED_POST_RE.match(line)
        if not match:
            continue
        subreddit, title = match.group(1).strip(), match.group(2).strip()
        if not title:
            continue
        posts.append(
            {
                "subreddit": subreddit or "reddit",
                "title": title,
                "url": "",
                "published": "",
            }
        )
    return posts


def _score_title_sentiment(title: str) -> int:
    """以詞界金融詞庫與簡易否定視窗計算單一標題的方向淨分。"""
    score = 0
    tokens = _EN_WORD_RE.findall(title.lower())
    for idx, token in enumerate(tokens):
        window = tokens[max(0, idx - 2) : idx]
        negated = any(prev in _EN_NEGATORS for prev in window)
        if token in _EN_BULLISH_TERMS:
            score += -1 if negated else 1
        elif token in _EN_BEARISH_TERMS:
            score += 1 if negated else -1

    for term in _ZH_BULLISH_TERMS:
        start = 0
        while True:
            pos = title.find(term, start)
            if pos < 0:
                break
            prefix = title[max(0, pos - 3) : pos]
            negated = any(neg in prefix for neg in _ZH_NEGATORS)
            score += -1 if negated else 1
            start = pos + len(term)

    for term in _ZH_BEARISH_TERMS:
        start = 0
        while True:
            pos = title.find(term, start)
            if pos < 0:
                break
            prefix = title[max(0, pos - 3) : pos]
            negated = any(neg in prefix for neg in _ZH_NEGATORS)
            score += 1 if negated else -1
            start = pos + len(term)

    return score


def _map_error_label(error_text: str) -> str:
    """將服務錯誤哨兵對映為具體的失敗狀態標籤（皆保留「抓取失敗」或「無法」關鍵字）。"""
    cleaned = error_text.strip()
    if cleaned == REDDIT_ERR_TUNNEL_UNSET:
        return "⚠️ 無法抓取 (未配置邊緣節點)"
    if cleaned == REDDIT_ERR_TIMEOUT:
        return "⚠️ 抓取失敗 (邊緣節點超時)"
    if cleaned == REDDIT_ERR_RATE_LIMITED:
        return "⚠️ 抓取失敗 (Reddit 上游限流)"
    if cleaned == REDDIT_ERR_UPSTREAM_HTTP:
        return "⚠️ 抓取失敗 (Reddit 上游異常)"
    return "⚠️ 抓取失敗 (邊緣節點異常)"


def classify_reddit_sentiment(
    reddit_text: Optional[str],
    reddit_posts: Optional[Sequence[Mapping[str, Any]]] = None,
) -> str:
    """根據貼文標題或服務錯誤狀態評定 Reddit 散戶情緒標籤。"""
    if not reddit_posts and is_reddit_error_text(reddit_text):
        return _map_error_label(str(reddit_text))

    is_cached = isinstance(reddit_text, str) and reddit_text.lstrip().startswith(
        REDDIT_CACHE_MARKER
    )
    suffix = f" {REDDIT_CACHE_MARKER}" if is_cached else ""

    titles: list[str] = []
    if reddit_posts:
        for post in reddit_posts:
            if isinstance(post, Mapping):
                title = str(post.get("title") or "").strip()
                if title:
                    titles.append(title)
    elif reddit_text:
        parsed_posts = _parse_cached_reddit_posts(reddit_text)
        if parsed_posts:
            titles = [p["title"] for p in parsed_posts]
        else:
            for raw_line in reddit_text.splitlines():
                line = raw_line.strip()
                if (
                    not line
                    or line == REDDIT_CACHE_MARKER
                    or line == REDDIT_NO_MATCH_MESSAGE
                    or is_reddit_error_text(line)
                ):
                    continue
                titles.append(line)

    if not titles:
        return f"⚖️ 中性{suffix}"

    total_score = sum(_score_title_sentiment(title) for title in titles)
    if total_score > 0:
        return f"🚀 樂觀 (Bullish){suffix}"
    if total_score < 0:
        return f"💀 恐慌 (Bearish){suffix}"
    return f"⚖️ 中性{suffix}"


def _map_edge_error_message(response_json: Mapping[str, Any]) -> str:
    """依 edge JSON 回應的 error_code（並相容舊版 data 文字）挑選具體錯誤訊息。"""
    error_code = str(response_json.get("error_code") or "").strip().lower()
    data_str = str(response_json.get("data") or "").lower()

    if error_code == "rate_limited" or "429" in data_str:
        return REDDIT_ERR_RATE_LIMITED
    if error_code in ("upstream_404", "upstream_403") or (
        "404" in data_str or "403" in data_str
    ):
        return REDDIT_ERR_UPSTREAM_HTTP
    if error_code == "timeout" or "timeout" in data_str or "超時" in data_str:
        return REDDIT_ERR_TIMEOUT
    return REDDIT_ERR_EDGE_GENERIC


async def _try_kv_cache_fallback(
    symbol: str, limit: int
) -> Optional[tuple[str, list[dict[str, str]]]]:
    """當邊緣抓取失敗時，嘗試從 kv_cache 讀取 36 小時內有效的每日批次快取。"""
    cached = await asyncio.to_thread(
        get_kv_cache_fresh,
        f"reddit_sentiment_{symbol.upper()}",
        REDDIT_CACHE_FALLBACK_MAX_AGE_SECONDS,
    )
    if not isinstance(cached, str):
        return None
    cleaned = cached.strip()
    if not cleaned or is_reddit_error_text(cleaned):
        return None
    parsed_posts = _parse_cached_reddit_posts(cleaned)[: max(limit, 0)]
    return f"{REDDIT_CACHE_MARKER}\n{cleaned}", parsed_posts


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
        fallback = await _try_kv_cache_fallback(symbol, limit)
        if fallback is not None:
            return fallback
        return REDDIT_ERR_TUNNEL_UNSET, empty_posts

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

            async with httpx.AsyncClient(
                timeout=45.0, follow_redirects=True, **edge_client_kwargs()
            ) as client:
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

                err_msg = _map_edge_error_message(response_json)
                logger.warning(
                    f"[{symbol}] 本地端爬取發生內部錯誤 "
                    f"[{response_json.get('error_code', 'unknown')}]: {response_json.get('data')}"
                )
                fallback = await _try_kv_cache_fallback(symbol, limit)
                if fallback is not None:
                    logger.info(f"[{symbol}] 已降級採用 kv_cache Reddit 情緒快取。")
                    return fallback
                return err_msg, empty_posts

    except httpx.ReadTimeout:
        logger.warning(f"[{symbol}] Tunnel 請求超時，本地端無回應。")
        fallback = await _try_kv_cache_fallback(symbol, limit)
        if fallback is not None:
            logger.info(
                f"[{symbol}] Tunnel 超時，已降級採用 kv_cache Reddit 情緒快取。"
            )
            return fallback
        return REDDIT_ERR_TIMEOUT, empty_posts
    except httpx.HTTPStatusError as e:
        status_code = e.response.status_code if e.response is not None else 0
        logger.warning(f"[{symbol}] 呼叫本地 Tunnel HTTP 錯誤 ({status_code}): {e}")
        fallback = await _try_kv_cache_fallback(symbol, limit)
        if fallback is not None:
            return fallback
        if status_code == 429:
            return REDDIT_ERR_RATE_LIMITED, empty_posts
        if status_code in (403, 404):
            return REDDIT_ERR_UPSTREAM_HTTP, empty_posts
        return REDDIT_ERR_CONNECTION, empty_posts
    except Exception as e:
        logger.warning(f"[{symbol}] 呼叫本地 Tunnel 失敗: {e}")
        fallback = await _try_kv_cache_fallback(symbol, limit)
        if fallback is not None:
            logger.info(f"[{symbol}] 連線異常，已降級採用 kv_cache Reddit 情緒快取。")
            return fallback
        return REDDIT_ERR_CONNECTION, empty_posts


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

            async with httpx.AsyncClient(
                timeout=45.0, follow_redirects=True, **edge_client_kwargs()
            ) as client:
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
