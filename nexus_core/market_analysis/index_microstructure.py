import httpx
import logging
import config
from typing import Dict, Optional

logger = logging.getLogger(__name__)


async def fetch_gex_metrics() -> Dict[str, float]:
    """呼叫邊緣爬蟲獲取大盤的 Gamma Flip Line 與 Put Wall 價位。"""
    import time
    import asyncio
    from database.cache import save_kv_cache, get_kv_cache

    fallback = {"spy_spot": 510.0, "gamma_flip": 515.0, "put_wall": 505.0}
    cache_key = "macro_gex_metrics_cache"

    async def _last_known_good_or_fallback() -> Dict[str, float]:
        try:
            cached_obj = await asyncio.to_thread(get_kv_cache, cache_key)
        except Exception as e:
            logger.warning(f"讀取 macro GEX 快取失敗: {e}")
            cached_obj = None
        if isinstance(cached_obj, dict) and isinstance(cached_obj.get("data"), dict):
            return {**cached_obj["data"], "_is_stale_cache": True}
        return fallback

    if not getattr(config, "TUNNEL_URL", ""):
        await save_kv_cache("macro_gex_is_fallback", 1)
        return await _last_known_good_or_fallback()
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
                    await save_kv_cache(
                        cache_key, {"data": gex_data, "timestamp": time.time()}
                    )
                    return gex_data  # type: ignore
    except Exception as e:
        logger.warning(f"無法從 Tunnel Scraper 獲取 GEX 數據: {e}")
    await save_kv_cache("macro_gex_is_fallback", 1)
    return await _last_known_good_or_fallback()


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
                    return liq_data  # type: ignore
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
                    return core_data  # type: ignore
    except Exception as e:
        logger.warning(f"無法從 Tunnel Scraper 獲取核心總經數據: {e}")
    await save_kv_cache("macro_core_is_fallback", 1)
    return fallback


async def _scrape_symbol_gex_raw(
    symbol: str, stale_cached_data: Optional[dict] = None
) -> dict:
    import time
    from database.cache import save_kv_cache

    cache_key = f"gex_metrics_{symbol.upper()}"
    fallback = {
        "spot": 0.0,
        "net_gex": 0.0,
        "call_wall": 0.0,
        "put_wall": 0.0,
        "gex_profile": {},
    }

    if not getattr(config, "TUNNEL_URL", ""):
        if stale_cached_data is not None:
            return {**stale_cached_data, "_is_stale_cache": True}
        return fallback

    try:
        base_url = config.TUNNEL_URL.rstrip("/")
        async with httpx.AsyncClient(timeout=15.0, follow_redirects=True) as client:
            res = await client.get(f"{base_url}/api/v1/scrape/options/{symbol}/gex")
            if res.status_code == 200:
                data = res.json()
                if data.get("status") == "success":
                    result_data = data.get("data", fallback)

                    # 檢查 API 傳回的 gex_profile 是否為空或全為 0
                    gex_prof = result_data.get("gex_profile", {})
                    is_valid_profile = False
                    if isinstance(gex_prof, dict) and gex_prof:
                        for v in gex_prof.values():
                            try:
                                if abs(float(v)) > 0.0001:
                                    is_valid_profile = True
                                    break
                            except Exception:
                                pass

                    # 若 API 傳回的 profile 無效 (盤前未刷新)，且我們有舊快取
                    if not is_valid_profile and stale_cached_data is not None:
                        stale_prof = stale_cached_data.get("gex_profile", {})
                        stale_valid = False
                        if isinstance(stale_prof, dict) and stale_prof:
                            for v in stale_prof.values():
                                try:
                                    if abs(float(v)) > 0.0001:
                                        stale_valid = True
                                        break
                                except Exception:
                                    pass
                        if stale_valid:
                            logger.info(
                                f"[{symbol}] API 回傳空 GEX 曝險，自動延展並使用上一份有效歷史快取"
                            )
                            result_data = stale_cached_data
                            result_data["_is_stale_cache"] = True

                    try:
                        await save_kv_cache(
                            cache_key, {"data": result_data, "timestamp": time.time()}
                        )
                    except Exception as e:
                        logger.warning(f"寫入 GEX 快取失敗 ({symbol}): {e}")
                    return result_data  # type: ignore
    except Exception as e:
        logger.warning(f"無法從 Tunnel Scraper 獲取 {symbol} GEX 數據: {e}")

    if stale_cached_data is not None:
        logger.warning(f"[{symbol}] API 不可用，回傳過期 GEX 快取資料作為降級備援。")
        return {**stale_cached_data, "_is_stale_cache": True}
    return fallback


async def fetch_symbol_gex_metrics(symbol: str) -> dict:
    """呼叫邊緣爬蟲獲取個股的 Net GEX, Call Wall, Put Wall 與 GEX Profile。"""
    import time
    import asyncio
    from database.cache import get_kv_cache, save_kv_cache
    from services.single_flight import SingleFlightManager

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

    # 優先讀取 edge 背景排程寫入的 GEX 快照（毫秒級 SQLite 讀取），
    # 命中且夠新鮮就直接採用，跳過下方即時 Playwright scrape。
    from services import edge_cache_client

    edge_cached = await edge_cache_client.get_cached_gex(symbol)
    if edge_cached is not None:
        edge_age = edge_cached.get("age_seconds")
        if edge_age is not None and edge_age < 3600:
            edge_data = edge_cached["data"]
            try:
                await save_kv_cache(
                    cache_key, {"data": edge_data, "timestamp": time.time()}
                )
            except Exception as e:
                logger.warning(f"寫入 GEX kv_cache 失敗 ({symbol}): {e}")
            return edge_data  # type: ignore

    # 若已有過期快取，立即以過期快取作為 SWR 回傳，並在背景非同步更新，避免阻塞主排程
    if stale_cached_data is not None:
        asyncio.create_task(
            SingleFlightManager.run(
                f"scrape_gex_{symbol.upper()}",
                _scrape_symbol_gex_raw,
                symbol,
                stale_cached_data,
            )
        )
        return {**stale_cached_data, "_is_stale_cache": True}

    # 初次冷啟動且無任何歷史快取時，透過 SingleFlight 執行防重疊查詢
    return await SingleFlightManager.run(  # type: ignore
        f"scrape_gex_{symbol.upper()}",
        _scrape_symbol_gex_raw,
        symbol,
        stale_cached_data,
    )


GEX_THIN_WALL_THRESHOLD: float = 500_000.0


def is_gex_wall_effective(
    wall_gex: float, threshold: float = GEX_THIN_WALL_THRESHOLD
) -> bool:
    """判定 GEX 牆體是否具備實質做市商深度（非單薄紙牆）。"""
    return abs(wall_gex) >= threshold


def find_overhead_negative_gex_swamp(
    gex_profile: dict, spot: float, min_negative_threshold: float = -5_000_000.0
) -> tuple[float, float]:
    """
    檢索現價上方最大的負 GEX 峰值（負 Gamma 泥淖）。
    若現價上方聚集龐大負 GEX（<= min_negative_threshold），反彈時做市商將順向拋售壓制。
    回傳: (swamp_strike, swamp_gex)，若無則回傳 (0.0, 0.0)
    """
    if not gex_profile or spot <= 0:
        return 0.0, 0.0
    swamp_strike = 0.0
    min_gex_val = 0.0
    for k, v in gex_profile.items():
        try:
            strike = float(k)
            gex = float(v)
            if strike > spot and gex <= min_negative_threshold:
                if gex < min_gex_val:
                    min_gex_val = gex
                    swamp_strike = strike
        except (ValueError, TypeError):
            continue
    return swamp_strike, min_gex_val


def calculate_positive_gex_depth_below(gex_profile: dict, spot: float) -> float:
    """
    計算現價下方所有履約價的正 GEX 總厚度。
    若數值極低 (< 500K)，代表現價下方做市商被動買盤緩衝極度枯竭，失守支撐易引發無量滑步暴跌。
    """
    if not gex_profile or spot <= 0:
        return 0.0
    total_pos = 0.0
    for k, v in gex_profile.items():
        try:
            strike = float(k)
            gex = float(v)
            if strike < spot and gex > 0:
                total_pos += gex
        except (ValueError, TypeError):
            continue
    return total_pos


def classify_gex_wall(
    strike_gex: float,
    max_positive_gex: float,
    is_heavy_otm_call: bool = False,
    min_effective_gex: float = 0.0,
) -> str:
    """
    GEX 與做市商意圖映射引擎 (GEX Mapping Engine)

    Args:
        strike_gex: 該履約價的 GEX 曝險值
        max_positive_gex: 該標的整體選擇權鏈中的最大正 GEX 值
        is_heavy_otm_call: 是否為深度價外大量 Call 的異常堆積
        min_effective_gex: 底牆最低有效深度門檻 (預設 0.0)

    Returns:
        str: 映射出的對沖屬性分類 (SUPPORT_GEX_WALL, THIN_SUPPORT_WALL, RESISTANCE_CALL_WALL, 或 NEUTRAL)
    """
    # 當 GEX 為正，且數值等於最大正 GEX 牆時，視為底牆
    if strike_gex > 0 and abs(strike_gex - max_positive_gex) < 1e-6:
        if min_effective_gex > 0 and strike_gex < min_effective_gex:
            return "THIN_SUPPORT_WALL"
        return "SUPPORT_GEX_WALL"  # 做市商護盤底牆 (逢低對沖買盤)

    # 當 GEX 為負 (造市商呈負 Gamma 需追漲殺跌)，或是出現價外 Call 異常堆積時，視為天花板壓制
    elif strike_gex < 0 or is_heavy_otm_call:
        return "RESISTANCE_CALL_WALL"  # 上方壓制天花板

    return "NEUTRAL"


def estimate_symbol_gamma_flip(gex_profile: dict, spot: float) -> float:
    """
    個股 Gamma Flip 輕量客戶端估算（累積 GEX 曝險零交叉點）。

    個股 GEX 端點（`fetch_symbol_gex_metrics`）目前不提供現成的 `gamma_flip`
    欄位（僅 SPY 總經端點 `/api/v1/scrape/macro/gex` 才有）。此函式複用已抓取
    的 `gex_profile`（履約價 -> GEX 曝險值）估算 Gamma Flip，不發動額外網路
    請求：依履約價由低到高排序，逐步累加 GEX 曝險，累積值由負轉正的履約價
    視為做市商由負轉正 Gamma 的臨界點估計值。

    這是輕量估算，非如 SPY 端點那樣與官方數據源比對的精算值。找不到交叉點
    （例如全數為正、全數為負，或 profile 為空/格式異常）一律回傳 0.0，
    由呼叫端 fail-safe 處理（視為無法確認，不應作為判斷依據）。
    """
    if not gex_profile:
        return 0.0
    try:
        sorted_strikes = sorted((float(k), float(v)) for k, v in gex_profile.items())
    except (ValueError, TypeError):
        return 0.0
    if not sorted_strikes:
        return 0.0

    cumulative = 0.0
    prev_cumulative: Optional[float] = None
    for strike, gex in sorted_strikes:
        cumulative += gex
        if prev_cumulative is not None and prev_cumulative < 0 <= cumulative:
            return strike
        prev_cumulative = cumulative
    return 0.0


def evaluate_escape_window_regime(
    prob: float | None = 0.50,
    cpi_dev: float = 0.0,
    wti: float = 75.0,
    vts_ratio: float = 0.88,
    is_negative_gamma: bool = False,
) -> tuple[int, int, str, int, str, str]:
    """
    評估四因子宏觀流動性矩陣與逃頂窗口狀態。

    Args:
        prob: FedWatch 維持高利率或加息機率 (0.0 ~ 1.0)
        cpi_dev: CPI 偏差值 (%)
        wti: WTI 原油價格
        vts_ratio: VIX 期限結構比例 (VIX / VIX3M)
        is_negative_gamma: 是否處於負 Gamma 踩踏區間

    Returns:
        tuple[int, int, str, int, str, str]:
            (tightening_score, easing_score, direction, shift_days, tier_title, short_status_desc)
    """
    try:
        safe_prob = float(prob) if prob is not None else 0.50
    except (ValueError, TypeError):
        safe_prob = 0.50

    tightening_score = 0
    easing_score = 0

    # Factor 1: FedWatch 利率定價
    if safe_prob > 0.70:
        tightening_score += 1
    elif safe_prob <= 0.40:
        easing_score += 1

    # Factor 2: 通膨與能源 (CPI / WTI)
    if (cpi_dev > 0.1) or (wti > 85.0):
        tightening_score += 1
    elif (cpi_dev <= 0.0) and (wti <= 80.0):
        easing_score += 1

    # Factor 3: VIX 期限結構 (VTS)
    if vts_ratio >= 1.0:
        tightening_score += 1
    elif vts_ratio < 0.90:
        easing_score += 1

    # Factor 4: 大盤微觀結構 Net GEX
    if is_negative_gamma:
        tightening_score += 1
    else:
        easing_score += 1

    # 三階矩陣狀態評估
    if tightening_score >= 2 or (safe_prob > 0.70 and is_negative_gamma):
        direction = "前移"
        shift_days = 8 if tightening_score >= 3 else 5
        tier_title = "🚨 收縮警戒 (Tightening Contraction)"
        short_status_desc = f"⚠️ 前移 {shift_days} 天 (高利率+結構承壓)"
    elif safe_prob <= 0.40 and easing_score >= 2 and tightening_score == 0:
        direction = "後推"
        shift_days = 5
        tier_title = "🟢 寬鬆擴張 (Liquidity Expansion)"
        short_status_desc = "🟢 後推 5 天 (流動性擴張)"
    else:
        direction = "維持"
        shift_days = 0
        tier_title = "🟡 中性平衡 (Neutral Balance)"
        if not is_negative_gamma and safe_prob > 0.70:
            short_status_desc = "🟢 正常窗口 (正Gamma護航中)"
        else:
            short_status_desc = "🟢 正常窗口 (均衡定價)"

    return (
        tightening_score,
        easing_score,
        direction,
        shift_days,
        tier_title,
        short_status_desc,
    )
