"""
services/edge_cache_client.py

集中封裝 nexus_core 對 nexus_edge_scraper 新增的「讀快取」端點呼叫
(`POST /api/v1/watchlist/sync`、`GET /api/v1/cache/gex/{symbol}`、
`GET /api/v1/cache/options/{symbol}/chain`)。

這些呼叫全部是**純附加的快速路徑**：edge 目前部署不穩定，任何一次呼叫
逾時、連不上、或回傳非 success 狀態，一律回傳 None（或就地吞掉錯誤），
由呼叫端無縫 fallback 回既有的 yfinance / edge 即時 scrape 行為，
不得讓既有功能對 edge 產生硬依賴。

理論上這些端點只是 edge 端 SQLite 的毫秒級讀取，因此統一使用較短的
timeout，比既有的即時 scrape 呼叫（15-30 秒）短得多。
"""

from typing import Any, Optional
import logging

import httpx

import config

logger = logging.getLogger(__name__)

_READ_TIMEOUT_SECONDS = 4.0
_SYNC_TIMEOUT_SECONDS = 5.0


def _base_url() -> str:
    return getattr(config, "TUNNEL_URL", "").rstrip("/")


async def sync_watchlist_symbols(symbols: list[str]) -> None:
    """Best-effort 同步目前全體使用者去重後的自選標的清單給 edge，
    讓 edge 的背景排程知道該輪詢哪些標的。失敗只記錄 warning，不拋出、
    不影響呼叫端（watchlist 心跳）繼續執行。"""
    base_url = _base_url()
    if not base_url or not symbols:
        return
    try:
        async with httpx.AsyncClient(timeout=_SYNC_TIMEOUT_SECONDS) as client:
            await client.post(
                f"{base_url}/api/v1/watchlist/sync",
                json={"symbols": symbols},
            )
    except Exception as e:
        logger.warning(f"同步 watchlist 標的清單至 edge 失敗（不影響心跳繼續執行): {e}")


async def get_cached_gex(symbol: str) -> Optional[dict[str, Any]]:
    """讀取 edge 背景排程快取的 GEX 快照。miss / 逾時 / edge 離線一律回傳
    None，呼叫端應無縫 fallback 回既有的即時抓取路徑。"""
    base_url = _base_url()
    if not base_url:
        return None
    try:
        async with httpx.AsyncClient(timeout=_READ_TIMEOUT_SECONDS) as client:
            res = await client.get(f"{base_url}/api/v1/cache/gex/{symbol}")
            if res.status_code == 200:
                data = res.json()
                if data.get("status") == "success" and isinstance(
                    data.get("data"), dict
                ):
                    return {
                        "data": data["data"],
                        "age_seconds": data.get("age_seconds"),
                    }
    except Exception as e:
        logger.info(f"[{symbol}] 讀取 edge GEX 快取失敗（將 fallback 至即時抓取): {e}")
    return None


async def get_cached_option_chain(
    symbol: str, expiry: Optional[str] = None
) -> Optional[dict[str, Any]]:
    """讀取 edge 背景排程快取的 Option Chain 快照。miss / 逾時 / edge 離線
    一律回傳 None，呼叫端應無縫 fallback 回既有的 yfinance / edge 即時
    scrape 路徑。"""
    base_url = _base_url()
    if not base_url:
        return None
    try:
        params = {"expiry": expiry} if expiry else None
        async with httpx.AsyncClient(timeout=_READ_TIMEOUT_SECONDS) as client:
            res = await client.get(
                f"{base_url}/api/v1/cache/options/{symbol}/chain", params=params
            )
            if res.status_code == 200:
                data = res.json()
                if data.get("status") == "success" and isinstance(
                    data.get("data"), dict
                ):
                    return {
                        "data": data["data"],
                        "age_seconds": data.get("age_seconds"),
                    }
    except Exception as e:
        logger.info(
            f"[{symbol}] 讀取 edge Option Chain 快取失敗（將 fallback 至即時抓取): {e}"
        )
    return None
