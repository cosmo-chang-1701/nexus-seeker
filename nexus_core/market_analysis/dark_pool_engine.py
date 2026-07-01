import httpx
import logging
import config
from typing import Dict, List, Any
from database.cache import save_kv_cache

logger = logging.getLogger(__name__)


async def fetch_and_cache_darkpool_dix() -> Dict[str, float]:
    """呼叫邊緣爬蟲獲取大盤 DIX 數據並寫入快取。"""
    fallback = {
        "dix": 45.2,
        "gex": 1.5,
    }

    if not getattr(config, "TUNNEL_URL", ""):
        await save_kv_cache("macro_darkpool_dix", fallback["dix"])
        return fallback

    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            res = await client.get(f"{config.TUNNEL_URL}/api/v1/scrape/darkpool")
            if res.status_code == 200:
                data = res.json()
                if data.get("status") == "success":
                    dp_data = data.get("data", fallback)
                    dix = float(dp_data.get("dix", 45.2))
                    await save_kv_cache("macro_darkpool_dix", dix)
                    return dp_data
    except Exception as e:
        logger.warning(f"無法從 Tunnel Scraper 獲取 DIX 數據: {e}")

    await save_kv_cache("macro_darkpool_dix", fallback["dix"])
    return fallback


async def fetch_darkpool_prints(symbol: str) -> Dict[str, Any]:
    """呼叫邊緣爬蟲獲取個股的近 24 小時前 5 大暗池大單"""
    fallback = {"symbol": symbol.upper(), "prints": []}

    if not getattr(config, "TUNNEL_URL", ""):
        return fallback

    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            res = await client.get(
                f"{config.TUNNEL_URL}/api/v1/scrape/darkpool/{symbol}"
            )
            if res.status_code == 200:
                data = res.json()
                if data.get("status") == "success":
                    return data.get("data", fallback)
    except Exception as e:
        logger.warning(f"無法從 Tunnel Scraper 獲取 {symbol} 暗池大宗明細: {e}")

    return fallback


def calculate_dark_pool_skew(prints: List[Dict[str, Any]]) -> float:
    """
    計算暗池偏斜度 (Dark Pool Skew)。
    買盤與賣盤的淨額比 (Net Premium)。若指標呈極端正值，代表機構強力護盤；負值為隱形天花板。
    """
    if not prints:
        return 0.0

    # Simple heuristic to determine if a print is "buy" or "sell" initiated based on VWAP or average.
    # For a real implementation, this would use bid/ask at trade time.
    avg_price = sum(p.get("price", 0.0) for p in prints) / len(prints)

    net_premium = 0.0
    total_premium = 0.0

    for p in prints:
        premium = float(p.get("premium", 0.0))
        price = float(p.get("price", 0.0))
        total_premium += premium

        if price >= avg_price:
            net_premium += premium
        else:
            net_premium -= premium

    if total_premium == 0:
        return 0.0

    skew = net_premium / total_premium
    return round(skew, 4)


def calculate_dp_poc(prints: List[Dict[str, Any]]) -> float:
    """
    DP-POC (Dark Pool Point of Control)
    從大宗交易中萃取出金額最大的一筆成交價，將其標定為「暗池磁吸價/阻力價」
    """
    if not prints:
        return 0.0

    max_print = max(prints, key=lambda x: x.get("premium", 0.0))
    return float(max_print.get("price", 0.0))
