"""
Finnhub Service — 集中式 Finnhub API client wrapper (Async Optimized)。

所有對 Finnhub REST API 的呼叫統一經過此模組，確保：
1. API Key 集中管理
2. Rate limiting（免費方案 60 calls/min, 使用 aiolimiter 控制）
3. 錯誤處理與 fallback
4. 回傳格式與既有程式碼相容（pandas DataFrame）
"""

import asyncio
import logging
import time
import json
import random
from datetime import datetime, timedelta
from typing import Optional, List, Dict, Any

import finnhub
import pandas as pd
import numpy as np
import yfinance as yf
from aiolimiter import AsyncLimiter

from config import FINNHUB_API_KEY
import database.financials as db_financials

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# 配置與 Rate Limiting (免費方案 60 calls/min)
# ---------------------------------------------------------------------------
# 設定每分鐘 55 次請求，保留 5 次緩衝以防網路重試導致的溢出
_limiter = AsyncLimiter(55, 60)

# Singleton client instance
_client: Optional[finnhub.Client] = None

def _get_client() -> finnhub.Client:
    """取得或初始化 Finnhub client (lazy singleton)。"""
    global _client
    if _client is None:
        if not FINNHUB_API_KEY:
            raise RuntimeError("FINNHUB_API_KEY 未設定，請在 .env 中配置")
        _client = finnhub.Client(api_key=FINNHUB_API_KEY)
        logger.info("Finnhub Client (Async Wrapper) 初始化完成")
    return _client

# ---------------------------------------------------------------------------
# Core Async API Call (Thread-safe Wrapper)
# ---------------------------------------------------------------------------
async def _execute_api_call(func, *args, **kwargs) -> Any:
    """
    執行 Finnhub API 呼叫的異步封裝。
    使用 aiolimiter 進行流量管制，並透過 asyncio.to_thread 避免阻塞事件循環。
    加入 Exponential Backoff 以自動處理 429 頻率限制。
    """
    max_retries = 3
    base_delay = 10.0

    for attempt in range(max_retries + 1):
        async with _limiter:
            try:
                # Finnhub SDK 為同步阻塞 I/O，必須在獨立線程中執行
                return await asyncio.to_thread(func, *args, **kwargs)
            except Exception as e:
                error_msg = str(e).lower()
                is_rate_limit = "429" in error_msg or "limit reached" in error_msg
                is_conn_error = "connection aborted" in error_msg or "timeout" in error_msg or "remotedisconnected" in error_msg
                if is_rate_limit or is_conn_error:
                    if attempt < max_retries:
                        # 指數退避，加入 jitter 避免同時重試
                        delay = base_delay * (2 ** attempt) + random.uniform(1, 3)
                        reason = "429 頻率限制" if is_rate_limit else "連線錯誤/超時"
                        logger.warning(f"🚨 觸發 Finnhub {reason}。將於 {delay:.1f} 秒後重試 (次數: {attempt + 1}/{max_retries})...")
                        await asyncio.sleep(delay)
                        continue
                    else:
                        reason = "429 頻率限制" if is_rate_limit else "連線錯誤/超時"
                        logger.error(f"🚨 觸發 Finnhub {reason}。已達最大重試次數，放棄呼叫。")
                        raise e
                raise e

# ---------------------------------------------------------------------------
# Quote (即時報價)
# ---------------------------------------------------------------------------
async def get_yfinance_quote(symbol: str) -> Dict[str, Any]:
    """使用 yfinance 取得即時報價，並轉換格式與 Finnhub 相容。"""
    yf_symbol = symbol if not symbol == "VIX" else "^VIX"
    try:
        ticker = yf.Ticker(yf_symbol)
        # 抓取最近 2 天資料以計算昨日收盤 (pc)
        df = await asyncio.to_thread(ticker.history, period="2d")
        if df.empty:
            logger.warning(f"[{yf_symbol}] yfinance quote 回傳資料為空")
            return {}
        
        latest = df.iloc[-1]
        prev_close = df.iloc[-2]['Close'] if len(df) > 1 else latest['Open']
        current_price = latest['Close']
        
        change = current_price - prev_close
        pct_change = (change / prev_close) * 100 if prev_close != 0 else 0.0
        
        return {
            'c': round(float(current_price), 2),
            'd': round(float(change), 2),
            'dp': round(float(pct_change), 4),
            'h': round(float(latest['High']), 2),
            'l': round(float(latest['Low']), 2),
            'o': round(float(latest['Open']), 2),
            'pc': round(float(prev_close), 2),
            't': int(df.index[-1].timestamp())
        }
    except Exception as e:
        logger.error(f"[{yf_symbol}] yfinance quote 失敗: {e}")
        return {}

async def get_quote(symbol: str) -> Dict[str, Any]:
    """取得即時報價 (非同步)。對於指數型標的，強制轉向 yfinance。"""
    if symbol.startswith('^') or symbol == 'VIX':
        return await get_yfinance_quote(symbol)

    client = _get_client()
    try:
        data = await _execute_api_call(client.quote, symbol)
        if data and data.get('c', 0) > 0:
            return data
        
        # 若 Finnhub 回傳無效或報權限錯誤 (c=0 有可能是權限問題或標的不存在)
        # 嘗試作為 fallback 轉向 yfinance
        logger.warning(f"[{symbol}] Finnhub quote 無效，嘗試 yfinance fallback")
        return await get_yfinance_quote(symbol)
    except Exception as e:
        # 如果是明確的權限錯誤，也轉向 yfinance
        error_msg = str(e).lower()
        if 'subscription required' in error_msg or 'market data' in error_msg:
             logger.info(f"[{symbol}] Finnhub 權限受限，強制轉向 yfinance")
             return await get_yfinance_quote(symbol)

        logger.error(f"[{symbol}] Finnhub quote 失敗: {e}")
        return {}

async def batch_get_quotes(symbols: List[str]) -> Dict[str, Dict[str, Any]]:
    """批次取得多檔標的的即時報價。"""
    tasks = [get_quote(sym) for sym in symbols]
    quotes = await asyncio.gather(*tasks)
    return {sym: q for sym, q in zip(symbols, quotes) if q}

# ---------------------------------------------------------------------------
# 歷史數據與指標 (yfinance)
# ---------------------------------------------------------------------------
async def get_history_df(symbol: str, period: str = "1y", interval: str = "1d") -> pd.DataFrame:
    """
    使用 yfinance 抓取歷史 K 線 (異步化)。
    """
    try:
        ticker = yf.Ticker(symbol)
        # yfinance 內部為同步，同樣使用 to_thread
        df = await asyncio.to_thread(ticker.history, period=period, interval=interval)
        
        if df.empty:
            logger.warning(f"[{symbol}] yfinance 歷史數據為空 (period={period}, interval={interval})")
            return pd.DataFrame()

        df.index.name = 'Date'
        if df.index.tz is not None:
            df.index = df.index.tz_localize(None)
            
        return df[['Open', 'High', 'Low', 'Close', 'Volume']]
    except Exception as e:
        logger.error(f"[{symbol}] yfinance 抓取失敗: {e}")
        return pd.DataFrame()

async def get_spy_history_df(period: str = "1y", interval: str = "1d", retries: int = 3) -> pd.DataFrame:
    """取得 SPY 基準歷史資料，針對暫時性鎖衝突進行重試。"""
    for attempt in range(retries):
        df = await get_history_df("SPY", period=period, interval=interval)
        if not df.empty:
            return df
        await asyncio.sleep(0.4 * (attempt + 1))

    logger.error(f"[SPY] 重試 {retries} 次後仍無法取得歷史資料")
    return pd.DataFrame()

# ---------------------------------------------------------------------------
# SMA 記憶體快取設定
# ---------------------------------------------------------------------------
_sma_cache = {} 
_SMA_CACHE_TTL = 28800  # 8 小時

async def get_sma(symbol: str, window: int = 200) -> Optional[float]:
    """計算簡單移動平均線 (SMA)。"""
    global _sma_cache
    current_time = time.time()
    cache_key = (symbol, window)

    if cache_key in _sma_cache:
        cached_val, expiry = _sma_cache[cache_key]
        if current_time < expiry:
            return cached_val

    try:
        period = "1y" if window <= 200 else "2y"
        df = await get_history_df(symbol, period=period)

        if df.empty or len(df) < window:
            return None

        sma_series = df['Close'].rolling(window=window).mean()
        current_sma = round(float(sma_series.iloc[-1]), 4)

        if not pd.isna(current_sma):
            _sma_cache[cache_key] = (current_sma, current_time + _SMA_CACHE_TTL)
        
        return current_sma if not pd.isna(current_sma) else None
    except Exception as e:
        logger.error(f"[{symbol}] 計算 SMA{window} 失敗: {e}")
        return None

def clear_sma_cache():
    global _sma_cache
    _sma_cache.clear()
    logger.info("Clarified SMA cache")

# ---------------------------------------------------------------------------
# EMA 記憶體快取設定
# ---------------------------------------------------------------------------
_ema_cache = {} 
_EMA_CACHE_TTL = 28800  # 8 小時

async def get_ema(symbol: str, window: int = 21) -> Optional[float]:
    """計算指數移動平均線 (EMA)。"""
    global _ema_cache
    now = time.time()
    cache_key = (symbol, window)

    if cache_key in _ema_cache:
        val, expiry = _ema_cache[cache_key]
        if now < expiry:
            return val

    try:
        period = "1mo" if window <= 21 else "1y"
        df = await get_history_df(symbol, period=period)

        if df.empty or len(df) < window:
            return None

        ema_series = df['Close'].ewm(span=window, adjust=False).mean()
        current_ema = round(float(ema_series.iloc[-1]), 4)

        if not np.isnan(current_ema):
            _ema_cache[cache_key] = (current_ema, now + _EMA_CACHE_TTL)
        return current_ema if not np.isnan(current_ema) else None
    except Exception as e:
        logger.error(f"[{symbol}] EMA{window} 計算失敗: {e}")
        return None

def clear_ema_cache():
    global _ema_cache
    _ema_cache.clear()
    logger.info("Clarified EMA cache")

# ---------------------------------------------------------------------------
# Basic Financials (具備 SQLite 持久化快取)
# ---------------------------------------------------------------------------
async def get_basic_financials(symbol: str, expiry_hours: int = 24) -> Dict[str, Any]:
    """取得基本面指標，優先從資料庫讀取快取。"""
    symbol = symbol.upper()

    # 1. 優先檢查 SQLite 持久化快取，並用 to_thread 避免阻塞 event loop
    cached_data = await asyncio.to_thread(db_financials.get_cached_financials, symbol, expiry_hours)
    if cached_data:
        return cached_data

    # 2. 快取失效，執行 API 請求
    client = _get_client()
    try:
        data = await _execute_api_call(client.company_basic_financials, symbol, 'all')
        metrics = data.get('metric', {}) if data else {}
        
        if metrics:
            # 3. 非同步寫入快取
            await asyncio.to_thread(db_financials.save_financials_cache, symbol, metrics)
            
        return metrics
    except Exception as e:
        logger.error(f"[{symbol}] Finnhub financials 失敗: {e}")
        return {}

async def get_dividend_yield(symbol: str) -> float:
    """取得年化股息殖利率。"""
    metrics = await get_basic_financials(symbol)
    yield_val = metrics.get('dividendYieldIndicatedAnnual', 0.0)
    if yield_val is None:
        return 0.0
    return round(float(yield_val) / 100.0, 4)

# ---------------------------------------------------------------------------
# Company Profile & ETF
# ---------------------------------------------------------------------------
async def get_company_profile(symbol: str) -> Dict[str, Any]:
    """取得公司/ETF 基本資料。"""
    client = _get_client()
    try:
        data = await _execute_api_call(client.company_profile2, symbol=symbol)
        return data if data else {}
    except Exception as e:
        logger.error(f"[{symbol}] Finnhub company profile 失敗: {e}")
        return {}

async def is_etf(symbol: str) -> bool:
    """判斷標的是否為 ETF。"""
    client = _get_client()
    try:
        data = await _execute_api_call(client.etfs_profile, symbol=symbol)
        if data and data.get('name'):
            return True
        return False
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
        if from_date is None:
            from_date = datetime.now().strftime('%Y-%m-%d')
        if to_date is None:
            to_date = (datetime.now() + timedelta(days=90)).strftime('%Y-%m-%d')

        data = await _execute_api_call(
            client.earnings_calendar,
            _from=from_date,
            to=to_date,
            symbol=symbol
        )
        return data.get('earningsCalendar', []) if data else []
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
            to_date = datetime.now().strftime('%Y-%m-%d')
        if from_date is None:
            from_date = (datetime.now() - timedelta(days=7)).strftime('%Y-%m-%d')

        data = await _execute_api_call(client.company_news, symbol, _from=from_date, to=to_date)
        if not data:
            return []

        import re
        cleaned_news = []
        seen_headlines = set()
        symbol_pattern = re.compile(rf'\b{re.escape(symbol)}\b', re.IGNORECASE)
        
        for item in data:
            headline = item.get('headline', '').strip()
            summary = item.get('summary', '').strip()
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
    try:
        # 同時啟動兩個非同步任務
        vix_task = get_history_df("^VIX", period="5d")
        oil_task = get_history_df("CL=F", period="5d")
        
        vix_df, oil_df = await asyncio.gather(vix_task, oil_task)
        
        if vix_df.empty or oil_df.empty:
            logger.warning("宏觀數據 (VIX/Oil) 抓取結果為空，使用預設值")
            return {"vix": 18.0, "oil": 75.0, "vix_change": 0.0}

        return {
            "vix": round(float(vix_df['Close'].iloc[-1]), 2),
            "oil": round(float(oil_df['Close'].iloc[-1]), 2),
            "vix_change": round(float(vix_df['Close'].pct_change().iloc[-1]), 4)
        }
    except Exception as e:
        logger.error(f"宏觀環境參數獲取失敗: {e}")
        return {"vix": 18.0, "oil": 75.0, "vix_change": 0.0}
