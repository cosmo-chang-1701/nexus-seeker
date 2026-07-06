import httpx
import logging
import config
from typing import Dict

logger = logging.getLogger(__name__)


async def fetch_gex_metrics() -> Dict[str, float]:
    """呼叫邊緣爬蟲獲取大盤的 Gamma Flip Line 與 Put Wall 價位。"""
    fallback = {"spy_spot": 510.0, "gamma_flip": 515.0, "put_wall": 505.0}
    from database.cache import save_kv_cache

    if not getattr(config, "TUNNEL_URL", ""):
        await save_kv_cache("macro_gex_is_fallback", 1)
        return fallback
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            res = await client.get(f"{config.TUNNEL_URL}/api/v1/scrape/macro/gex")
            if res.status_code == 200:
                data = res.json()
                if data.get("status") == "success":
                    gex_data = data["data"]
                    await save_kv_cache(
                        "macro_spy_spot", gex_data.get("spy_spot", 510.0)
                    )
                    await save_kv_cache(
                        "macro_spy_gamma_flip",
                        gex_data.get("gamma_flip", 515.0),
                    )
                    await save_kv_cache(
                        "macro_gamma_flip_line",
                        gex_data.get("gamma_flip", 515.0) * 10.0,
                    )
                    await save_kv_cache("macro_gex_is_fallback", 0)
                    return gex_data
    except Exception as e:
        logger.warning(f"無法從 Tunnel Scraper 獲取 GEX 數據: {e}")
    await save_kv_cache("macro_gex_is_fallback", 1)
    return fallback


async def get_market_regime() -> str:
    """根據 VIX、VTS 比率以及 SPY 現貨價與零 Gamma 線的相對位置，判讀當前市場 Regime。"""
    from services.market_data_service import (
        get_macro_environment,
        get_vix_term_structure,
        get_quote,
    )

    # 1. 抓取大盤微觀結構 GEX 數據
    gex_data = await fetch_gex_metrics()
    gamma_flip_raw = gex_data.get("gamma_flip")
    gamma_flip = float(gamma_flip_raw) if gamma_flip_raw is not None else 515.0

    # 2. 獲取 VIX 數值
    try:
        macro = await get_macro_environment()
        vix_raw = macro.get("vix")
        vix = float(vix_raw) if vix_raw is not None else 18.0
    except Exception as e:
        logger.warning(f"獲取 VIX 指標失敗: {e}")
        vix = 18.0

    # 3. 獲取 VTS 期限結構
    try:
        vts = await get_vix_term_structure()
        vts_ratio_raw = vts.get("vts_ratio")
        vts_ratio = float(vts_ratio_raw) if vts_ratio_raw is not None else 0.95
    except Exception as e:
        logger.warning(f"獲取 VIX 期限結構失敗: {e}")
        vts_ratio = 0.95

    # 4. 獲取 SPY 現貨價
    try:
        spy_quote = await get_quote("SPY")
        spy_spot_raw = spy_quote.get("c") if spy_quote else None
        spy_spot = float(spy_spot_raw) if spy_spot_raw is not None else 0.0
        if spy_spot <= 0.0:
            spy_spot_gex = gex_data.get("spy_spot")
            spy_spot = float(spy_spot_gex) if spy_spot_gex is not None else 510.0
    except Exception as e:
        logger.warning(f"獲取 SPY 即時報價失敗: {e}")
        spy_spot_gex = gex_data.get("spy_spot")
        spy_spot = float(spy_spot_gex) if spy_spot_gex is not None else 510.0

    # 5. Regime 條件判定 (繁體中文回傳說明，內部邏輯以英文代號)
    # 獲取跨資產流動性指標
    try:
        liquidity = await fetch_liquidity_metrics()
        ted_spread = liquidity.get("ted_spread", 0.0)
    except Exception as e:
        logger.warning(f"獲取流動性指標失敗: {e}")
        ted_spread = 0.0

    # 系統性流動性危機 (TED Spread > 0.5 且處於 Negative Gamma)
    # 這裡 0.5 (50 bps) 為 TED Spread 歷史上的警戒水位
    if ted_spread > 0.5 and spy_spot < gamma_flip:
        return "SYSTEMIC_LIQUIDITY_CRISIS"

    # 條件：VIX > 20 且 vts_ratio >= 1.0 (Backwardation) 且 SPY 現貨價 < Gamma Flip Line
    if vix > 20.0 and vts_ratio >= 1.0 and spy_spot < gamma_flip:
        return "SHORT_GAMMA_CRITICAL"

    return "NORMAL"


async def fetch_liquidity_metrics() -> dict:
    """呼叫邊緣爬蟲獲取 TED Spread, SOFR, DTB3 與 High Yield Spread 等跨資產流動性指標。"""
    fallback = {
        "ted_spread": 0.15,
        "sofr_90": 5.3,
        "dtb3": 5.15,
        "high_yield_spread": 3.1,
    }
    from database.cache import save_kv_cache

    if not getattr(config, "TUNNEL_URL", ""):
        await save_kv_cache("macro_liquidity_is_fallback", 1)
        return fallback
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            res = await client.get(f"{config.TUNNEL_URL}/api/v1/scrape/macro/liquidity")
            if res.status_code == 200:
                data = res.json()
                if data.get("status") == "success":
                    liq_data = data.get("data", fallback)
                    await save_kv_cache(
                        "macro_ted_spread", liq_data.get("ted_spread", 0.15)
                    )
                    await save_kv_cache("macro_liquidity_is_fallback", 0)
                    return liq_data
    except Exception as e:
        logger.warning(f"無法從 Tunnel Scraper 獲取流動性數據: {e}")
    await save_kv_cache("macro_liquidity_is_fallback", 1)
    return fallback


async def fetch_core_macro_metrics() -> dict:
    """呼叫邊緣爬蟲獲取 RRP, Fed Balance, UER, Sahm Rule, Fear & Greed 等核心總經指標。"""
    fallback = {
        "rrp": 420.5,
        "fed_balance": 7.25,
        "uer": 4.0,
        "sahm_rule": 0.35,
        "fear_greed": 48.0,
    }
    from database.cache import save_kv_cache

    if not getattr(config, "TUNNEL_URL", ""):
        await save_kv_cache("macro_core_is_fallback", 1)
        return fallback
    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            res = await client.get(
                f"{config.TUNNEL_URL}/api/v1/scrape/macro/core_metrics"
            )
            if res.status_code == 200:
                data = res.json()
                if data.get("status") == "success":
                    core_data = data.get("data", fallback)
                    await save_kv_cache("macro_rrp", core_data.get("rrp", 420.5))
                    await save_kv_cache(
                        "macro_rrp_change_30d", core_data.get("rrp_change_30d", 5.0)
                    )
                    await save_kv_cache(
                        "macro_fed_balance", core_data.get("fed_balance", 7.25)
                    )
                    await save_kv_cache("macro_uer", core_data.get("uer", 4.0))
                    await save_kv_cache(
                        "macro_sahm_rule", core_data.get("sahm_rule", 0.35)
                    )
                    await save_kv_cache(
                        "macro_fear_greed", core_data.get("fear_greed", 48.0)
                    )
                    await save_kv_cache("macro_core_is_fallback", 0)
                    return core_data
    except Exception as e:
        logger.warning(f"無法從 Tunnel Scraper 獲取核心總經數據: {e}")
    await save_kv_cache("macro_core_is_fallback", 1)
    return fallback


async def fetch_symbol_gex_metrics(symbol: str) -> dict:
    """呼叫邊緣爬蟲獲取個股的 Net GEX, Call Wall, Put Wall 與 GEX Profile。"""
    import time
    import asyncio
    from database.cache import get_kv_cache, save_kv_cache

    cache_key = f"gex_metrics_{symbol.upper()}"
    stale_cached_data: dict | None = None
    try:
        cached_obj = await asyncio.to_thread(get_kv_cache, cache_key)
        if cached_obj and isinstance(cached_obj, dict):
            data = cached_obj.get("data")
            if isinstance(data, dict):
                # 快取有效期設定為 4 小時 (14400 秒)
                if time.time() - cached_obj.get("timestamp", 0) < 14400:
                    return data
                # 快取已過期，保留作為 API 失敗時的降級備援
                stale_cached_data = data
    except Exception as e:
        logger.warning(f"讀取 GEX 快取失敗 ({symbol}): {e}")

    fallback = {
        "spot": 0.0,
        "net_gex": 0.0,
        "call_wall": 0.0,
        "put_wall": 0.0,
        "gex_profile": {},
    }

    if not getattr(config, "TUNNEL_URL", ""):
        if stale_cached_data is not None:
            logger.warning(f"[{symbol}] TUNNEL_URL 未設定，回傳過期 GEX 快取資料。")
            return {**stale_cached_data, "_is_stale_cache": True}
        return fallback
    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            res = await client.get(
                f"{config.TUNNEL_URL}/api/v1/scrape/options/{symbol}/gex"
            )
            if res.status_code == 200:
                data = res.json()
                if data.get("status") == "success":
                    result_data = data.get("data", fallback)
                    try:
                        await save_kv_cache(
                            cache_key, {"data": result_data, "timestamp": time.time()}
                        )
                    except Exception as e:
                        logger.warning(f"寫入 GEX 快取失敗 ({symbol}): {e}")
                    return result_data
    except Exception as e:
        logger.warning(f"無法從 Tunnel Scraper 獲取 {symbol} GEX 數據: {e}")

    if stale_cached_data is not None:
        logger.warning(f"[{symbol}] API 不可用，回傳過期 GEX 快取資料作為降級備援。")
        return {**stale_cached_data, "_is_stale_cache": True}
    return fallback
