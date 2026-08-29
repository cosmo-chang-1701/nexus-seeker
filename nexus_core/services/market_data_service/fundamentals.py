"""market_data_service：基本面 (Financials/Profile/ETF)、行事曆、新聞與總經指標。"""

from typing import Any, Dict, List, Optional, cast
import asyncio
import logging
import math
import time
from datetime import datetime, timedelta

import database.financials as db_financials
from services.market_data_service._core import (
    _execute_api_call,
    _get_client,
    _sanitize_ticker,
)
from services.market_data_service.caches import (
    _etf_cache,
    _ETF_CACHE_TTL,
    _option_chain_cache,
    _profile_cache,
    _PROFILE_CACHE_TTL,
)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Basic Financials (具備 SQLite 持久化快取)
# ---------------------------------------------------------------------------
async def get_basic_financials(symbol: str, expiry_hours: int = 24) -> Dict[str, Any]:
    """取得基本面指標，優先從資料庫讀取快取。"""
    symbol = _sanitize_ticker(symbol)

    # 1. 優先檢查 SQLite 持久化快取，並用 to_thread 避免阻塞 event loop
    cached_data = await asyncio.to_thread(
        db_financials.get_cached_financials, symbol, expiry_hours
    )
    if cached_data:
        return cached_data

    # 2. 快取失效，執行 API 請求
    client = _get_client()
    try:
        data = await _execute_api_call(client.company_basic_financials, symbol, "all")
        metrics: Dict[str, Any] = (
            cast(Dict[str, Any], data.get("metric", {})) if data else {}
        )

        if metrics:
            # 3. 非同步寫入快取
            await asyncio.to_thread(
                db_financials.save_financials_cache, symbol, metrics
            )

        return metrics
    except Exception as e:
        logger.error(f"[{symbol}] Finnhub financials 失敗: {e}")
        return {}


async def get_dividend_yield(symbol: str) -> float:
    """取得年化股息殖利率。"""
    metrics = await get_basic_financials(symbol)
    yield_val = metrics.get("dividendYieldIndicatedAnnual", 0.0)
    if yield_val is None:
        return 0.0
    return round(float(yield_val) / 100.0, 4)


# ---------------------------------------------------------------------------
# Company Profile & ETF
# ---------------------------------------------------------------------------
async def get_company_profile(symbol: str) -> Dict[str, Any]:
    """取得公司/ETF 基本資料。"""
    symbol = _sanitize_ticker(symbol)
    now = time.time()
    if symbol in _profile_cache:
        val, expiry = _profile_cache[symbol]
        if now < expiry:
            return val  # type: ignore

    client = _get_client()
    try:
        data = await _execute_api_call(client.company_profile2, symbol=symbol)
        res: Dict[str, Any] = cast(Dict[str, Any], data) if data else {}
        if res:
            _profile_cache[symbol] = (res, now + _PROFILE_CACHE_TTL)
        return res
    except Exception as e:
        logger.error(f"[{symbol}] Finnhub company profile 失敗: {e}")
        return {}


async def is_etf(symbol: str) -> bool:
    """判斷標的是否為 ETF。"""
    symbol = _sanitize_ticker(symbol)
    now = time.time()
    if symbol in _etf_cache:
        val, expiry = _etf_cache[symbol]
        if now < expiry:
            return val  # type: ignore

    client = _get_client()
    try:
        data = await _execute_api_call(client.etfs_profile, symbol=symbol)
        res = False
        if data and data.get("name"):
            res = True
        _etf_cache[symbol] = (res, now + _ETF_CACHE_TTL)
        return res
    except Exception:
        return False


# ---------------------------------------------------------------------------
# Economic Calendar (經濟行事曆)
async def get_economic_calendar(from_date: str, to_date: str) -> List[Dict[str, Any]]:
    """獲取經濟行事曆資料。"""
    try:
        client = _get_client()
        data = await _execute_api_call(
            client.calendar_economic, _from=from_date, to=to_date
        )
        return (
            cast(List[Dict[str, Any]], data.get("economicCalendar", [])) if data else []
        )
    except Exception as e:
        logger.error(f"Finnhub economic calendar 失敗: {e}")
        return []


# Earnings Calendar (財報日期)
# ---------------------------------------------------------------------------
async def get_earnings_calendar(
    symbol: str,
    from_date: Optional[str] = None,
    to_date: Optional[str] = None,
) -> List[Dict[str, Any]]:
    """取得財報日曆。"""
    client = _get_client()
    try:
        if from_date is None:
            from_date = datetime.now().strftime("%Y-%m-%d")
        if to_date is None:
            to_date = (datetime.now() + timedelta(days=90)).strftime("%Y-%m-%d")

        data = await _execute_api_call(
            client.earnings_calendar, _from=from_date, to=to_date, symbol=symbol
        )
        earnings = data.get("earningsCalendar", []) if data else []
        earnings.sort(key=lambda x: x.get("date", ""))
        return cast(List[Dict[str, Any]], earnings)
    except Exception as e:
        logger.error(f"[{symbol}] Finnhub earnings calendar 失敗: {e}")
        return []


# ---------------------------------------------------------------------------
# Company News (公司新聞)
# ---------------------------------------------------------------------------
async def get_company_news(
    symbol: str,
    from_date: Optional[str] = None,
    to_date: Optional[str] = None,
    limit: int = 5,
) -> List[Dict[str, Any]]:
    """取得公司新聞。"""
    client = _get_client()
    try:
        if to_date is None:
            to_date = datetime.now().strftime("%Y-%m-%d")
        if from_date is None:
            from_date = (datetime.now() - timedelta(days=7)).strftime("%Y-%m-%d")

        data = await _execute_api_call(
            client.company_news, symbol, _from=from_date, to=to_date
        )
        if not data:
            return []

        import re

        cleaned_news = []
        seen_headlines = set()
        symbol_pattern = re.compile(rf"\b{re.escape(symbol)}\b", re.IGNORECASE)

        for item in data:
            headline = item.get("headline", "").strip()
            summary = item.get("summary", "").strip()
            if not headline:
                continue
            hl_lower = headline.lower()
            if hl_lower in seen_headlines:
                continue
            content_text = f"{headline} {summary}"
            if not symbol_pattern.search(content_text):
                continue
            seen_headlines.add(hl_lower)
            cleaned_news.append(item)

        return cleaned_news[:limit]
    except Exception as e:
        logger.error(f"[{symbol}] Finnhub company news 失敗: {e}")
        return []


# ---------------------------------------------------------------------------
# Macro Environment (異步併發優化)
# ---------------------------------------------------------------------------
async def get_macro_environment() -> Dict[str, float]:
    """併發獲獲取 VIX 與原油數據。"""
    # 延遲從套件頂層 import get_history_df：讓 `patch("services.market_data_service.get_history_df")`
    # 能正確攔截這裡的內部呼叫（get_history_df 定義於 history.py，跨子模組呼叫若在
    # 檔案頂層綁定會繞過套件層級的 monkeypatch，理由同 quote.py 內部處理）。
    from services.market_data_service import get_history_df

    try:
        # 同時啟動兩個非同步任務
        vix_task = get_history_df("^VIX", period="5d")
        oil_task = get_history_df("CL=F", period="5d")

        vix_df, oil_df = await asyncio.gather(vix_task, oil_task)

        if vix_df.empty or oil_df.empty:
            logger.warning("宏觀數據 (VIX/Oil) 抓取結果為空，使用預設值")
            return {"vix": 18.0, "oil": 75.0, "vix_change": 0.0}

        vix_val = float(vix_df["Close"].iloc[-1])
        oil_val = float(oil_df["Close"].iloc[-1])
        vix_change_val = float(vix_df["Close"].pct_change().iloc[-1])

        return {
            "vix": round(vix_val, 2) if not math.isnan(vix_val) else 18.0,
            "oil": round(oil_val, 2) if not math.isnan(oil_val) else 75.0,
            "vix_change": round(vix_change_val, 4)
            if not math.isnan(vix_change_val)
            else 0.0,
        }
    except Exception as e:
        logger.error(f"宏觀環境參數獲取失敗: {e}")
        return {"vix": 18.0, "oil": 75.0, "vix_change": 0.0}


async def get_vix_term_structure() -> Dict[str, Any]:
    """取得 VIX 期限結構 (以 ^VIX / ^VIX3M 為代理)。"""
    # 延遲從套件頂層 import：理由同 get_macro_environment()。
    from services.market_data_service import get_history_df

    try:
        vix_task = get_history_df("^VIX", period="5d")
        vix3m_task = get_history_df("^VIX3M", period="5d")
        vix_df, vix3m_df = await asyncio.gather(vix_task, vix3m_task)

        if vix_df.empty or vix3m_df.empty:
            logger.warning("VIX 或 VIX3M 歷史數據為空，無法計算 VTS 期限結構")
            return {
                "vts_ratio": 0.0,
                "vts_state": "UNKNOWN",
                "vix_front": None,
                "vix_back": None,
                "is_valid": False,
            }

        vix_close = float(vix_df["Close"].iloc[-1])
        vix3m_close = float(vix3m_df["Close"].iloc[-1])

        # 數據合理性驗證 (VIX 與 VIX3M 歷史常態在 5.0 ~ 150.0 之間)
        if (
            math.isnan(vix_close)
            or math.isnan(vix3m_close)
            or vix_close < 5.0
            or vix_close > 150.0
            or vix3m_close < 5.0
            or vix3m_close > 150.0
        ):
            logger.warning(
                f"VIX 期限結構數據異常 (VIX: {vix_close}, VIX3M: {vix3m_close})，放棄計算"
            )
            return {
                "vts_ratio": 0.0,
                "vts_state": "UNKNOWN",
                "vix_front": None,
                "vix_back": None,
                "is_valid": False,
            }

        vts_ratio = round(vix_close / vix3m_close, 3)
        state = "Backwardation" if vts_ratio >= 1.0 else "Contango"
        return {
            "vts_ratio": vts_ratio,
            "vts_state": state,
            "vix_front": round(vix_close, 2),
            "vix_back": round(vix3m_close, 2),
            "is_valid": True,
        }
    except Exception as e:
        logger.error(f"VIX 期限結構計算失敗: {e}")
        return {
            "vts_ratio": 0.0,
            "vts_state": "UNKNOWN",
            "vix_front": None,
            "vix_back": None,
            "is_valid": False,
        }


async def get_vix_zscores() -> Dict[str, float]:
    """取得 VIX 30天與60天 Z-Score"""
    # 延遲從套件頂層 import：理由同 get_macro_environment()。
    from services.market_data_service import get_history_df

    try:
        # 取得至少 60 天以上的營業日，約需 90 個真實日曆天
        df = await get_history_df("^VIX", period="6mo")
        if df.empty or len(df) < 60:
            return {"zscore_30": 0.0, "zscore_60": 0.0}

        current_vix = float(df["Close"].iloc[-1])

        # 30 day z-score
        mean_30 = float(df["Close"].tail(30).mean())
        std_30 = float(df["Close"].tail(30).std())
        z_30 = (current_vix - mean_30) / std_30 if std_30 > 0.01 else 0.0

        # 60 day z-score
        mean_60 = float(df["Close"].tail(60).mean())
        std_60 = float(df["Close"].tail(60).std())
        z_60 = (current_vix - mean_60) / std_60 if std_60 > 0.01 else 0.0

        return {"zscore_30": round(z_30, 2), "zscore_60": round(z_60, 2)}
    except Exception as e:
        logger.error(f"VIX Z-score 計算失敗: {e}")
        return {"zscore_30": 0.0, "zscore_60": 0.0}


def check_and_reconcile_max_pain_anomaly(
    symbol: str, max_pain: float, spot_price: float
) -> bool:
    """
    Check if the Max Pain price deviates from the spot price by more than 30%.
    If so, record a warning log, mark database cache as stale, trigger background revalidation, and return True.
    """
    if spot_price <= 0.0 or max_pain <= 0.0:
        return False

    deviation = abs(max_pain - spot_price) / spot_price
    if deviation > 0.30:
        logger.warning(
            f"🚨 [Max Pain Anomaly Alert] For {symbol}, Max Pain (${max_pain:.2f}) "
            f"deviates from spot price (${spot_price:.2f}) by {deviation:.2%} (> 30%). "
            f"Marking cache as stale and triggering background revalidation..."
        )
        try:
            from database import mark_market_cache_stale

            # Mark stale in DB instead of deleting
            mark_market_cache_stale(symbol)

            # Trigger background revalidation task
            async def _async_revalidate_max_pain() -> None:
                try:
                    logger.info(
                        f"🔄 [SWR] Background revalidating option chain/max pain for {symbol}..."
                    )
                    # Fetch fresh option chain and force update of the cache
                    from market_analysis.sentiment_engine import SentimentEngine

                    # Clear memory cache for this symbol first to force a fresh pull in background task
                    from market_analysis.sentiment_engine import _iv_cache

                    if symbol.upper() in _iv_cache:
                        del _iv_cache[symbol.upper()]
                    keys_to_del = [
                        k
                        for k in _option_chain_cache.keys()
                        if k[0].upper() == symbol.upper()
                    ]
                    for k in keys_to_del:
                        del _option_chain_cache[k]

                    # Clear SQLite KV cache for the symbol's Max Pain
                    import sqlite3
                    import config

                    try:
                        with sqlite3.connect(config.DB_NAME) as conn:
                            cursor = conn.cursor()
                            cursor.execute(
                                "DELETE FROM kv_cache WHERE key LIKE ?",
                                (f"max_pain_{symbol.upper()}%",),
                            )
                            conn.commit()
                    except Exception as db_err:
                        logger.warning(
                            f"Failed to clear SQLite KV cache for {symbol} Max Pain: {db_err}"
                        )

                    # Re-run calculate_max_pain with _retry=True to bypass cache check and pull fresh options data
                    res = await SentimentEngine.calculate_max_pain(symbol, _retry=True)
                    if res and not res.get("error"):
                        logger.info(
                            f"✅ [SWR] Background revalidation completed for {symbol}: Max Pain = {res.get('max_pain')}"
                        )
                except Exception as ex:
                    logger.error(
                        f"❌ [SWR] Background revalidation failed for {symbol}: {ex}"
                    )

            # Trigger background revalidation
            asyncio.create_task(_async_revalidate_max_pain())

            return True  # Anomaly detected and handled via SWR
        except Exception as e:
            logger.error(f"Failed to mark cache as stale: {e}")
    return False
