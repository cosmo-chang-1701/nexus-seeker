"""market_data_service：基本面 (Financials/Profile/ETF)、行事曆、新聞與總經指標。"""

from typing import Any, Dict, List, Optional, cast
import asyncio
import logging
import math
import time
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import database.financials as db_financials
from services.market_data_service._core import (
    _execute_api_call,
    _get_client,
    _sanitize_ticker,
)
from services.market_data_service.caches import (
    BoundedCache,
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


_DIVIDEND_YIELD_CACHE_TTL: float = 24 * 3600.0
_dividend_yield_cache: Any = BoundedCache(max_size=500)


async def get_dividend_yield_strict(symbol: str) -> Optional[float]:
    """取得「實際」年化股息率 (小數)；無法取得時回傳 None。

    1. Finnhub `dividendYieldIndicatedAnnual` (個股)。
    2. 缺值時 (ETF 多數不在 Finnhub basic financials 內) 改以 yfinance 近 12 個月
       實際配息總額 / 最新收盤價計算。
    兩者皆失敗回傳 None，由呼叫端標示「股息率未知」——不再以 ETF 一律 1.5%
    之類的常數冒充。真的不配息的標的回傳 0.0 (已知為零)。
    """
    from services.market_data_service import get_history_df
    from services.market_data_service._core import _to_yfinance_symbol, call_yf

    symbol = _sanitize_ticker(symbol)
    now = time.time()
    if symbol in _dividend_yield_cache:
        val, expiry = _dividend_yield_cache[symbol]
        if now < expiry:
            return cast(Optional[float], val)

    result: Optional[float] = None
    try:
        metrics = await get_basic_financials(symbol)
        raw = metrics.get("dividendYieldIndicatedAnnual") if metrics else None
        if raw is not None:
            result = round(float(raw) / 100.0, 4)
    except Exception as e:
        logger.debug(f"[{symbol}] Finnhub 股息率讀取失敗: {e}")

    if result is None:
        try:
            import yfinance as yf
            import pandas as pd

            ticker = yf.Ticker(_to_yfinance_symbol(symbol))
            divs = await call_yf(lambda: ticker.dividends)
            df = await get_history_df(symbol, "1y")
            price = (
                float(df["Close"].iloc[-1]) if df is not None and not df.empty else 0.0
            )
            if divs is not None and price > 0:
                idx = pd.to_datetime(divs.index)
                if getattr(idx, "tz", None) is not None:
                    idx = idx.tz_localize(None)
                cutoff = pd.Timestamp(datetime.now()) - pd.Timedelta(days=365)
                ttm = float(divs[idx >= cutoff].sum()) if len(divs) else 0.0
                result = round(ttm / price, 4)
        except Exception as e:
            logger.warning(f"[{symbol}] yfinance 近 12 月配息抓取失敗，股息率未知: {e}")

    if result is not None:
        _dividend_yield_cache[symbol] = (result, now + _DIVIDEND_YIELD_CACHE_TTL)
    return result


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
        ny_tz = ZoneInfo("America/New_York")
        now_ny = datetime.now(ny_tz)
        if from_date is None:
            from_date = now_ny.strftime("%Y-%m-%d")
        if to_date is None:
            to_date = (now_ny + timedelta(days=90)).strftime("%Y-%m-%d")

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
async def get_macro_environment() -> Dict[str, Any]:
    """併發取得 VIX 與原油數據；任一項未知時該欄位為 None（**不補備援常數**）。

    回傳 `{"vix": float|None, "oil": float|None, "vix_change": float|None}`：
    - `vix` 取自 `get_vix_spot_strict()`（即時 quote），與原油互相獨立——原油
      抓不到不再拖累 VIX 一起退回 18.0。
    - `vix_change` 為 VIX 相對昨收的變動率（小數，0.05 = +5%）。
    - 呼叫端必須自行處理 None（fail-closed 或在畫面上標示「資料不足」）。
    """
    from services.market_data_service import get_history_df

    async def _oil() -> Optional[float]:
        try:
            oil_df = await get_history_df("CL=F", period="5d")
            if oil_df is None or oil_df.empty:
                return None
            oil_val = float(oil_df["Close"].iloc[-1])
            if math.isnan(oil_val) or oil_val <= 0:
                return None
            return round(oil_val, 2)
        except Exception as e:
            logger.warning(f"原油 (CL=F) 抓取失敗，回傳 None: {e}")
            return None

    vix_snapshot, oil_val = await asyncio.gather(_vix_quote_snapshot(), _oil())
    vix_val, vix_change = vix_snapshot
    if vix_val is None:
        logger.warning("宏觀數據：VIX 未知 (不補 18.0 備援值)")
    if oil_val is None:
        logger.warning("宏觀數據：原油價格未知 (不補 75.0 備援值)")
    return {"vix": vix_val, "oil": oil_val, "vix_change": vix_change}


async def _vix_quote_snapshot() -> tuple[Optional[float], Optional[float]]:
    """以即時 quote 取得 (VIX 現值, 相對昨收變動率)；任一失敗回傳 None。"""
    from services.market_data_service import get_quote

    try:
        quote = await get_quote("^VIX")
    except Exception as e:
        logger.warning(f"VIX 即時報價抓取失敗，回傳 None: {e}")
        return None, None
    if not quote:
        return None, None
    try:
        vix_val = float(quote.get("c") or 0.0)
    except (TypeError, ValueError):
        return None, None
    if math.isnan(vix_val) or vix_val <= 0:
        return None, None
    change: Optional[float] = None
    try:
        pc = float(quote.get("pc") or 0.0)
        if pc > 0 and not math.isnan(pc):
            change = round((vix_val - pc) / pc, 4)
    except (TypeError, ValueError):
        change = None
    return round(vix_val, 2), change


async def get_vix_spot_strict() -> Optional[float]:
    """取得 VIX 即時值（quote）；任何失敗或空資料回傳 None。

    全 repo 的 VIX 消費端統一走這裡（或 `get_macro_environment()["vix"]`，
    兩者同源），未知時回傳 None，由呼叫端 fail-closed——不得以 18.0 冒充
    真實值（18.0 落在 Ready 階梯，會讓部位照常放大）。過去取自 6 小時快取的
    日線收盤，盤中可能是前一日的舊值；現改用 15 秒快取的即時 quote。
    """
    vix_val, _change = await _vix_quote_snapshot()
    return vix_val


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


async def check_and_reconcile_max_pain_anomaly(
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
            await mark_market_cache_stale(symbol)

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
                    # 這裡刻意不用 sqlite3.connect() 直寫：那會繞過
                    # DatabaseWriteQueue，並在 event loop 執行緒上阻塞。
                    from database.connection import execute_write_async

                    try:
                        await execute_write_async(
                            "DELETE FROM kv_cache WHERE key LIKE ?",
                            (f"max_pain_{symbol.upper()}%",),
                        )
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
