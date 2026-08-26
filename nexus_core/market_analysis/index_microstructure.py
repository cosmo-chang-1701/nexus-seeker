import httpx
import logging
import time
import config
from typing import Dict, Optional

logger = logging.getLogger(__name__)

# get_market_regime() 快取：該值為全域、與使用者無關的單一市況判讀，但過去完全
# 未快取，導致動態轉倉引擎單一使用者單一 30 分鐘週期內最多被獨立呼叫 4 次
# (Scenario 2/4/5)，且 intraday_pipeline.py 的自選股心跳評估每檔標的又各自呼叫
# 一次，每次呼叫皆會觸發 fetch_gex_metrics()/fetch_liquidity_metrics() 兩支未快取
# 的邊緣爬蟲 HTTP 端點。TTL 刻意設定較短 (60 秒)：此值會直接影響
# SHORT_GAMMA_CRITICAL/SYSTEMIC_LIQUIDITY_CRISIS 期間凍結買方進場等真實交易安全
# 機制，過長的 TTL 會讓危機偵測延遲生效。
_MARKET_REGIME_CACHE_TTL: float = 60.0
_market_regime_cache_value: Optional[str] = None
_market_regime_cache_expiry: float = 0.0

# fetch_core_macro_metrics() 快取：所有現有呼叫端皆非次秒級時效需求 (排程本身即
# 每 30 分鐘一次，其餘呼叫端僅在 KV 快取為空時才觸發 fallback)，故給予較長 TTL。
_CORE_MACRO_METRICS_CACHE_TTL: float = 150.0
_core_macro_metrics_cache_value: Optional[dict] = None
_core_macro_metrics_cache_expiry: float = 0.0

# get_spx_capped_from_above_signal() 快取：動態轉倉引擎 Scenario 5 的 Covered
# Call Overlay 分支每 30 分鐘週期對每一檔符合條件的 CORE 持倉評估一次，此訊號
# 本身與個別持倉無關 (純大盤 SPY 結構判讀)，TTL 刻意比 get_market_regime()
# 寬鬆 (300 秒，比照 _STRUCTURAL_SIGNALS_CACHE_TTL 的設計理由)：此訊號僅用於
# 門控一個防禦性加碼收租建議，並非即時進場閘門，不需要 60 秒等級的新鮮度，
# 過短的 TTL 只會讓同一輪次內每檔 CORE 持倉各自重複觸發一次 UOA 掃描。
_SPX_CAPPED_SIGNAL_CACHE_TTL: float = 300.0
_spx_capped_signal_cache_value: Optional[dict] = None
_spx_capped_signal_cache_expiry: float = 0.0


def invalidate_market_regime_cache() -> None:
    """清除 get_market_regime() 的記憶體快取，供 /force_macro_update 等手動刷新
    流程於成功刷新 GEX/流動性數據後呼叫，避免管理員手動強制刷新後仍看到過期的
    市況判讀結果長達一個 TTL 週期。"""
    global _market_regime_cache_value, _market_regime_cache_expiry
    _market_regime_cache_value = None
    _market_regime_cache_expiry = 0.0


def invalidate_core_macro_metrics_cache() -> None:
    """清除 fetch_core_macro_metrics() 的記憶體快取，語意同 invalidate_market_regime_cache()。"""
    global _core_macro_metrics_cache_value, _core_macro_metrics_cache_expiry
    _core_macro_metrics_cache_value = None
    _core_macro_metrics_cache_expiry = 0.0


def invalidate_spx_capped_from_above_signal_cache() -> None:
    """清除 get_spx_capped_from_above_signal() 的記憶體快取，語意同
    invalidate_market_regime_cache()。"""
    global _spx_capped_signal_cache_value, _spx_capped_signal_cache_expiry
    _spx_capped_signal_cache_value = None
    _spx_capped_signal_cache_expiry = 0.0


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
    """根據 VIX、VTS 比率以及 SPY 現貨價與零 Gamma 線的相對位置，判讀當前市場 Regime。

    全域記憶體快取 (TTL=_MARKET_REGIME_CACHE_TTL)：該值與呼叫端使用者無關，透過
    SingleFlightManager 防止快取未命中時的併發重複抓取。詳見模組頂部快取變數註解。
    """
    global _market_regime_cache_value, _market_regime_cache_expiry

    now = time.time()
    if _market_regime_cache_value is not None and now < _market_regime_cache_expiry:
        return _market_regime_cache_value

    from services.single_flight import SingleFlightManager

    regime = await SingleFlightManager.run(
        "get_market_regime", _compute_market_regime_uncached
    )
    _market_regime_cache_value = regime
    _market_regime_cache_expiry = time.time() + _MARKET_REGIME_CACHE_TTL
    return regime  # type: ignore


async def _compute_market_regime_uncached() -> str:
    """get_market_regime() 的實際運算邏輯 (無快取)，供快取層與 SingleFlight 呼叫。"""
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


_FEAR_GREED_EXTREME_FEAR_BOUND: float = 25.0
_FEAR_GREED_EXTREME_GREED_BOUND: float = 75.0

# 核心資金部署引擎 (Dynamic Rollover Scenario 5) 的總經自動建議機制，統一分為四級
# 市況分級 (CRISIS / EXTREME_FEAR / EXTREME_GREED / NORMAL)。suggest_boxx_allocation_pct()
# 與 suggest_target_allocation_pct() 皆透過 _resolve_core_deployment_macro_tier()
# 取得同一份分級結果，確保兩者的建議值永遠基於完全一致的市況輸入 (regime +
# fear_greed 只評估一次)，結構上不可能互相矛盾。
_BOXX_SUGGEST_BY_TIER: Dict[str, float] = {
    "CRISIS": 70.0,
    "EXTREME_FEAR": 60.0,
    "EXTREME_GREED": 20.0,
    "NORMAL": 30.0,  # 其餘正常市況，維持偏向投入候選標的的既有行為
}
# target_allocation_pct 建議值語意與 boxx_allocation_pct 相反方向連動：市況越差，
# 越傾向續抱防禦性核心部位（建議的目標配置上限越高，觸發部署的門檻也越高）；
# 一旦真的觸發部署，超額資金才由 boxx_allocation_pct 決定優先停泊 BOXX 還是追價
# 候選標的。兩者共用同一份分級，方向設計上彼此呼應而非衝突。
_TARGET_ALLOC_SUGGEST_BY_TIER: Dict[str, float] = {
    "CRISIS": 70.0,
    "EXTREME_FEAR": 60.0,
    "EXTREME_GREED": 30.0,
    "NORMAL": 50.0,  # 常見的核心/衛星 50/50 中性基準配置
}


async def _resolve_core_deployment_macro_tier() -> str:
    """評估核心資金部署總經自動建議機制所使用的統一市況分級，回傳
    "CRISIS" | "EXTREME_FEAR" | "EXTREME_GREED" | "NORMAL" 其中之一。"""
    try:
        regime = await get_market_regime()
    except Exception as e:
        logger.warning(f"評估核心資金部署總經建議值時取得市場 Regime 失敗: {e}")
        regime = "NORMAL"

    if regime in ("SYSTEMIC_LIQUIDITY_CRISIS", "SHORT_GAMMA_CRITICAL"):
        return "CRISIS"

    try:
        core_metrics = await fetch_core_macro_metrics()
        fear_greed = float(core_metrics.get("fear_greed", 48.0))
    except Exception as e:
        logger.warning(f"評估核心資金部署總經建議值時取得 Fear & Greed 指數失敗: {e}")
        fear_greed = 48.0

    if fear_greed <= _FEAR_GREED_EXTREME_FEAR_BOUND:
        return "EXTREME_FEAR"
    if fear_greed >= _FEAR_GREED_EXTREME_GREED_BOUND:
        return "EXTREME_GREED"

    return "NORMAL"


async def suggest_boxx_allocation_pct() -> float:
    """依當前大盤 Gamma Regime 與 Fear & Greed 指數，評估動態轉倉引擎核心資金部署
    (Dynamic Rollover Scenario 5, CORE_DEPLOYMENT) 超額資金轉入 BOXX 防禦的建議
    閾值 (0-100)。供使用者未透過 /edit_holding 手動設定 boxx_allocation_pct 時的
    自動預設依據，數值 >= _BOXX_DEFENSE_THRESHOLD (50.0) 代表建議優先防禦轉入 BOXX。
    """
    tier = await _resolve_core_deployment_macro_tier()
    return _BOXX_SUGGEST_BY_TIER[tier]


async def suggest_target_allocation_pct() -> float:
    """依當前大盤 Gamma Regime 與 Fear & Greed 指數，評估 CORE 持倉 (如 VOO)
    target_allocation_pct 的參考建議值 (0-100)。**僅供 /list_holdings 顯示參考，
    不會被核心資金部署引擎自動套用生效**——target_allocation_pct 是 CORE_DEPLOYMENT
    是否觸發的嚴格 opt-in 閘門，刻意不比照 boxx_allocation_pct 自動代入計算，避免
    從未透過 /edit_holding 表態過的使用者被意外觸發部署 (見 core_deployment.py 的
    opt-in 閘門設計說明)。"""
    tier = await _resolve_core_deployment_macro_tier()
    return _TARGET_ALLOC_SUGGEST_BY_TIER[tier]


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
    """呼叫邊緣爬蟲獲取 RRP, Fed Balance, UER, Sahm Rule, Fear & Greed 等核心總經指標。

    全域記憶體快取 (TTL=_CORE_MACRO_METRICS_CACHE_TTL)，機制同 get_market_regime()。
    """
    global _core_macro_metrics_cache_value, _core_macro_metrics_cache_expiry

    now = time.time()
    if (
        _core_macro_metrics_cache_value is not None
        and now < _core_macro_metrics_cache_expiry
    ):
        return _core_macro_metrics_cache_value

    from services.single_flight import SingleFlightManager

    result = await SingleFlightManager.run(
        "fetch_core_macro_metrics", _fetch_core_macro_metrics_uncached
    )
    _core_macro_metrics_cache_value = result
    _core_macro_metrics_cache_expiry = time.time() + _CORE_MACRO_METRICS_CACHE_TTL
    return result  # type: ignore


async def _fetch_core_macro_metrics_uncached() -> dict:
    """fetch_core_macro_metrics() 的實際運算邏輯 (無快取)，供快取層與 SingleFlight 呼叫。"""
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


async def fetch_symbol_gex_metrics(symbol: str, force_live: bool = False) -> dict:
    """呼叫邊緣爬蟲獲取個股的 Net GEX, Call Wall, Put Wall 與 GEX Profile。

    force_live=True 時完全略過 4 小時 kv_cache 與 Edge Snapshot 兩層快取，直接
    進行即時 Playwright scrape（仍透過 SingleFlight 防止併發重複掃描）；若即時
    抓取失敗仍會優雅降級回退至舊快取而非直接失敗。僅供已透過 Discord defer
    （不受 3 秒互動逾時限制）的深度分析路徑使用。"""
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
                if (
                    not force_live
                    and time.time() - cached_obj.get("timestamp", 0) < 14400
                ):
                    return data
                # 快取已過期（或 force_live 要求略過），保留作為 API 失敗時的降級備援
                stale_cached_data = data
    except Exception as e:
        logger.warning(f"讀取 GEX 快取失敗 ({symbol}): {e}")

    if not force_live:
        # 優先讀取 edge 背景排程寫入的 GEX 快照（毫秒級 SQLite 讀取），
        # 命中且夠新鮮就直接採用，跳過下方即時 Playwright scrape。
        from services import edge_cache_client
        from services.market_data_service import _EDGE_SNAPSHOT_MAX_AGE_SECONDS

        edge_cached = await edge_cache_client.get_cached_gex(symbol)
        if edge_cached is not None:
            edge_age = edge_cached.get("age_seconds")
            # 與選擇權鏈共用同一顆 edge 背景輪詢器（約 30 分鐘輪完一輪），
            # 故沿用同一個新鮮度閾值常數，避免兩處各自維護不同步的數值。
            if edge_age is not None and edge_age < _EDGE_SNAPSHOT_MAX_AGE_SECONDS:
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

    # force_live=True：不使用任何快取捷徑，直接同步等待即時掃描結果。
    # 非 force_live 且無任何歷史快取（冷啟動）：透過 SingleFlight 執行防重疊查詢。
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


def detect_uoa_sto_call_physical_cap(
    uoa_list: list, spot: float, ratio_threshold: float = 1.0
) -> tuple[bool, float]:
    """掃描 UOA (異常期權活動) 清單，偵測現價上方是否存在單筆 ratio (成交量/未平倉量)
    超過 ratio_threshold 的 STO Call 物理封頂 (即機構單筆巨量賣出開倉 Call，物理上
    鎖死上方空間)。回傳 (has_physical_cap, capping_strike)，找不到則為 (False, 0.0)。

    抽自 dynamic_rollover/opportunity_cost.py 的
    _confirm_entry_condition3_no_physical_cap (僅取其 STO ratio 封頂偵測邏輯，
    不含該函式另外疊加的 Call Wall 緊貼現價判定 —— 那部分是個股特有的
    call_wall 概念，SPY/SPX 等大盤代理標的沒有對應語意)，供該函式與
    get_spx_capped_from_above_signal() 共用，確保「什麼算 STO 物理封頂」在
    個股進場確認與大盤結構訊號兩處定義一致。刻意放在 index_microstructure.py
    而非 dynamic_rollover/ 內，維持既有的單向依賴方向
    (dynamic_rollover 已經 import index_microstructure，反向則不然)。
    """
    for entry in uoa_list:
        if not isinstance(entry, dict):
            continue
        if str(entry.get("type", "")).upper() != "CALL":
            continue
        if "STO" not in str(entry.get("action", "")):
            continue
        strike = float(entry.get("strike", 0.0) or 0.0)
        ratio = float(entry.get("ratio", 0.0) or 0.0)
        if strike > spot and ratio > ratio_threshold:
            return True, strike
    return False, 0.0


async def get_spx_capped_from_above_signal() -> dict:
    """判讀大盤 SPX (以 SPY 為可交易代理標的) 是否「受制於上方負 Gamma 泥淖與
    STO 封頂、缺乏向上爆發力」—— 動態轉倉引擎 Scenario 5 Covered Call Overlay
    分支 (核心持倉加碼賣出備兌買權收租) 的門控訊號。

    觸發條件 (三者同時成立)：
    1. get_market_regime() == "NORMAL"（危機模式下不建議賣方策略，交由
       Scenario 4 槓桿防禦處理）。
    2. SPY 選擇權鏈存在現價上方的負 Gamma 泥淖 (find_overhead_negative_gex_swamp)。
    3. SPY 存在現價上方的 STO Call 物理封頂 (detect_uoa_sto_call_physical_cap)。

    回傳 dict：
    {
        "is_capped": bool,
        "regime": str,
        "swamp_strike": float,       # 0.0 代表未偵測到
        "swamp_gex": float,
        "has_uoa_physical_cap": bool,
        "capping_strike": float,     # 0.0 代表未偵測到
        "reason": str,                # 逐項判定說明，供呼叫端組裝 reason 文字
    }

    全域記憶體快取 (TTL=_SPX_CAPPED_SIGNAL_CACHE_TTL)，機制同 get_market_regime()。
    """
    global _spx_capped_signal_cache_value, _spx_capped_signal_cache_expiry

    now = time.time()
    if (
        _spx_capped_signal_cache_value is not None
        and now < _spx_capped_signal_cache_expiry
    ):
        return _spx_capped_signal_cache_value

    from services.single_flight import SingleFlightManager

    result = await SingleFlightManager.run(
        "get_spx_capped_from_above_signal",
        _compute_spx_capped_from_above_signal_uncached,
    )
    _spx_capped_signal_cache_value = result
    _spx_capped_signal_cache_expiry = time.time() + _SPX_CAPPED_SIGNAL_CACHE_TTL
    return result  # type: ignore


async def _compute_spx_capped_from_above_signal_uncached() -> dict:
    """get_spx_capped_from_above_signal() 的實際運算邏輯 (無快取)。"""
    regime = await get_market_regime()

    swamp_strike = 0.0
    swamp_gex = 0.0
    has_uoa_physical_cap = False
    capping_strike = 0.0

    if regime == "NORMAL":
        try:
            gex_data = await fetch_symbol_gex_metrics("SPY")
            spy_spot = float(gex_data.get("spot", 0.0) or 0.0)
            gex_profile = gex_data.get("gex_profile")
            if spy_spot > 0 and isinstance(gex_profile, dict):
                swamp_strike, swamp_gex = find_overhead_negative_gex_swamp(
                    gex_profile, spy_spot
                )
        except Exception as e:
            logger.warning(f"[SPX 結構訊號] SPY GEX Profile 抓取失敗: {e}")
            spy_spot = 0.0

        if swamp_strike > 0:
            try:
                from market_analysis.sentiment.uoa_detector import detect_uoa

                uoa_list = await detect_uoa("SPY")
                has_uoa_physical_cap, capping_strike = detect_uoa_sto_call_physical_cap(
                    list(uoa_list) if uoa_list else [], spy_spot
                )
            except Exception as e:
                logger.warning(f"[SPX 結構訊號] SPY UOA 抓取失敗: {e}")

    is_capped = regime == "NORMAL" and swamp_strike > 0 and has_uoa_physical_cap

    reason_parts = [f"大盤 Regime: `{regime}`"]
    if swamp_strike > 0:
        reason_parts.append(f"SPY 上方負 Gamma 泥淖 @ ${swamp_strike:.2f}")
    else:
        reason_parts.append("SPY 上方未偵測到負 Gamma 泥淖")
    if has_uoa_physical_cap:
        reason_parts.append(f"SPY STO Call 物理封頂 @ ${capping_strike:.2f}")
    else:
        reason_parts.append("SPY 未偵測到 STO Call 物理封頂")

    return {
        "is_capped": is_capped,
        "regime": regime,
        "swamp_strike": swamp_strike,
        "swamp_gex": swamp_gex,
        "has_uoa_physical_cap": has_uoa_physical_cap,
        "capping_strike": capping_strike,
        "reason": "，".join(reason_parts),
    }


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
