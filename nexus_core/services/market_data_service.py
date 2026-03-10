"""
Finnhub Service — 集中式 Finnhub API client wrapper。

所有對 Finnhub REST API 的呼叫統一經過此模組，確保：
1. API Key 集中管理
2. Rate limiting（免費方案 60 calls/min）
3. 錯誤處理與 fallback
4. 回傳格式與既有程式碼相容（pandas DataFrame）
"""

import time
import logging
import finnhub
import pandas as pd
import numpy as np
import yfinance as yf
from datetime import datetime, timedelta
from typing import Optional, List, Dict, Any

from config import FINNHUB_API_KEY

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Singleton client instance
# ---------------------------------------------------------------------------
_client: Optional[finnhub.Client] = None

# Rate limiting: 免費方案上限 60 calls/min
_RATE_LIMIT_INTERVAL = 1.05  # 每次呼叫間最少間隔 (秒), 略高於 1s 以保安全
_last_call_time: float = 0.0


def _get_client() -> finnhub.Client:
    """取得或初始化 Finnhub client (lazy singleton)。"""
    global _client
    if _client is None:
        if not FINNHUB_API_KEY:
            raise RuntimeError("FINNHUB_API_KEY 未設定，請在 .env 中配置")
        _client = finnhub.Client(api_key=FINNHUB_API_KEY)
        logger.info("Finnhub client 初始化完成")
    return _client


def _rate_limit():
    """簡易 rate limiter，確保呼叫間隔不低於 _RATE_LIMIT_INTERVAL。"""
    global _last_call_time
    elapsed = time.time() - _last_call_time
    if elapsed < _RATE_LIMIT_INTERVAL:
        time.sleep(_RATE_LIMIT_INTERVAL - elapsed)
    _last_call_time = time.time()


# ---------------------------------------------------------------------------
# Quote (即時報價)
# ---------------------------------------------------------------------------
def get_quote(symbol: str) -> Dict[str, Any]:
    """
    取得即時報價。

    Returns:
        dict with keys: c (current), d (change), dp (change_pct),
        h (high), l (low), o (open), pc (previous_close), t (timestamp)
    """
    _rate_limit()
    try:
        client = _get_client()
        data = client.quote(symbol)
        if data and data.get('c', 0) > 0:
            return data
        logger.warning(f"[{symbol}] Finnhub quote 回傳無效資料: {data}")
        return {}
    except Exception as e:
        logger.error(f"[{symbol}] Finnhub quote 失敗: {e}")
        return {}


def batch_get_quotes(symbols: List[str]) -> Dict[str, Dict[str, Any]]:
    """
    批次取得多檔標的的即時報價。

    Returns:
        dict[symbol] -> quote_data
    """
    results = {}
    for sym in symbols:
        quote = get_quote(sym)
        if quote:
            results[sym] = quote
    return results

def _period_to_timestamps(period: str) -> tuple[int, int]:
    """
    將 yfinance 風格的 period 字串轉換為 (from_ts, to_ts)。

    Supported: '1d', '5d', '1mo', '3mo', '6mo', '1y', '2y', '5y', '60d', '90d'
    """
    to_ts = int(time.time())
    period_lower = period.lower()

    period_map = {
        '1d': timedelta(days=1),
        '5d': timedelta(days=5),
        '1mo': timedelta(days=30),
        '3mo': timedelta(days=90),
        '6mo': timedelta(days=180),
        '1y': timedelta(days=365),
        '2y': timedelta(days=730),
        '5y': timedelta(days=1825),
    }

    # 處理如 "60d", "90d" 的自訂天數格式
    if period_lower in period_map:
        delta = period_map[period_lower]
    elif period_lower.endswith('d') and period_lower[:-1].isdigit():
        delta = timedelta(days=int(period_lower[:-1]))
    else:
        logger.warning(f"未知 period 格式 '{period}'，預設使用 1y")
        delta = timedelta(days=365)

    from_ts = int((datetime.now() - delta).timestamp())
    return from_ts, to_ts

def get_history_df(symbol: str, period: str = "1y") -> pd.DataFrame:
    """
    [High-CP Path] 放棄 Finnhub Candles，回歸 yfinance 抓取歷史 K 線。
    
    優點：
    1. 100% 避開 Finnhub 403 Forbidden 權限問題（如 CRCL）。
    2. 不消耗 Finnhub 每分鐘 60 次的 API 配額，留給 Quote 與 Financials。
    3. yfinance 在歷史數據的覆蓋率遠高於 Finnhub 免費版。
    """
    try:
        # 🚀 僅使用 yfinance 抓取歷史 DataFrame
        ticker = yf.Ticker(symbol)
        df = ticker.history(period=period)

        if df.empty:
            logger.warning(f"[{symbol}] yfinance 歷史數據為空")
            return pd.DataFrame()

        # 🚀 格式標準化 (Standardization)
        # 1. 統一 Index 名稱為 'Date'
        df.index.name = 'Date'
        
        # 2. 移除時區資訊 (Timezone-naive)，避免與後續計算 (如 Greeks) 衝突
        if df.index.tz is not None:
            df.index = df.index.tz_localize(None)

        # 3. 僅保留 NRO 核心計算所需的五個欄位
        valid_columns = ['Open', 'High', 'Low', 'Close', 'Volume']
        df = df[valid_columns]

        return df

    except Exception as e:
        logger.error(f"[{symbol}] yfinance 歷史資料抓取失敗: {e}")
        return pd.DataFrame()

# ---------------------------------------------------------------------------
# SMA 記憶體快取設定
# ---------------------------------------------------------------------------
_sma_cache = {} 
_SMA_CACHE_TTL = 28800  # 快取存活時間：8 小時 (秒)

def get_sma(symbol: str, window: int = 200) -> Optional[float]:
    r"""
    計算簡單移動平均線 (Simple Moving Average)。
    數學定義: $$SMA = \frac{1}{n} \sum_{i=1}^{n} P_i$$
    記憶體快取機制，減少對 yfinance 的重複請求。
    """
    global _sma_cache
    current_time = time.time()
    cache_key = (symbol, window)

    # 檢查快取是否存在且未過期
    if cache_key in _sma_cache:
        cached_val, expiry = _sma_cache[cache_key]
        if current_time < expiry:
            logger.info(f"⚡ [Cache Hit] {symbol} SMA{window}: {cached_val}")
            return cached_val

    try:
        logger.info(f"🌐 [Cache Miss] 正在從 yfinance 抓取 {symbol} 歷史數據計算 SMA{window}...")
        # 對於 SMA 200，建議抓取 1y 或 2y 以確保有足夠的 Trading Days
        period = "1y" if window <= 200 else "2y"
        df = get_history_df(symbol, period=period)

        if df.empty or len(df) < window:
            logger.warning(f"[{symbol}] 樣本數不足 ({len(df)} < {window})，無法計算 SMA")
            return None

        # 計算 Rolling Mean 並取得最新觀測值
        sma_series = df['Close'].rolling(window=window).mean()
        current_sma = round(float(sma_series.iloc[-1]), 4)

        if pd.isna(current_sma):
            return None

        # 寫入快取
        _sma_cache[cache_key] = (current_sma, current_time + _SMA_CACHE_TTL)
        
        return current_sma

    except Exception as e:
        logger.error(f"[{symbol}] 計算 SMA{window} 失敗: {e}")
        return None

def clear_sma_cache():
    """手動清除快取 (例如在開盤前執行)"""
    global _sma_cache
    _sma_cache.clear()
    logger.info("🧹 SMA 快取已清空")

# ---------------------------------------------------------------------------
# EMA 記憶體快取設定
# ---------------------------------------------------------------------------
_ema_cache = {} 
_EMA_CACHE_TTL = 28800  # 8 小時 (與 SMA 同步)

def get_ema(symbol: str, window: int = 21) -> Optional[float]:
    """
    計算指數移動平均線 (Exponential Moving Average)。
    公式: $$EMA_t = [P_t \times \alpha] + [EMA_{t-1} \times (1 - \alpha)]$$
    其中 \alpha = 2 / (window + 1)
    """
    global _ema_cache
    now = time.time()
    cache_key = (symbol, window)

    # 1. 檢查快取
    if cache_key in _ema_cache:
        val, expiry = _ema_cache[cache_key]
        if now < expiry:
            logger.info(f"⚡ [EMA Cache Hit] {symbol} EMA{window}: {val}")
            return val

    # 2. 實體計算
    try:
        logger.info(f"🌐 [EMA Cache Miss] 正在計算 {symbol} EMA{window}...")
        # 為確保 EMA 準確性，抓取長度建議為 window 的 3 倍以上
        period = "1mo" if window <= 21 else "1y"
        df = get_history_df(symbol, period=period)

        if df.empty or len(df) < window:
            logger.warning(f"[{symbol}] 樣本數不足，無法計算 EMA{window}")
            return None

        # 使用 pandas ewm (Exponential Weighted Moving Average)
        # span 參數對應 window，adjust=False 確保使用遞歸定義
        ema_series = df['Close'].ewm(span=window, adjust=False).mean()
        current_ema = round(float(ema_series.iloc[-1]), 4)

        if np.isnan(current_ema):
            return None

        # 3. 寫入快取
        _ema_cache[cache_key] = (current_ema, now + _EMA_CACHE_TTL)
        return current_ema

    except Exception as e:
        logger.error(f"[{symbol}] EMA{window} 計算失敗: {e}")
        return None

def clear_ema_cache():
    """手動清空 EMA 快取"""
    global _ema_cache
    _ema_cache.clear()
    logger.info("🧹 EMA 快取已清空")

# ---------------------------------------------------------------------------
# Basic Financials (基本面指標)
# ---------------------------------------------------------------------------
def get_basic_financials(symbol: str) -> Dict[str, Any]:
    """
    取得基本面指標 (dividend yield, beta, 52W high/low 等)。

    Returns:
        dict of metric values (e.g. 'dividendYieldIndicatedAnnual', 'beta', etc.)
    """
    _rate_limit()
    try:
        client = _get_client()
        data = client.company_basic_financials(symbol, 'all')
        return data.get('metric', {}) if data else {}
    except Exception as e:
        logger.error(f"[{symbol}] Finnhub basic financials 失敗: {e}")
        return {}


def get_dividend_yield(symbol: str) -> float:
    """取得年化股息殖利率。"""
    metrics = get_basic_financials(symbol)
    # Finnhub 欄位名: 'dividendYieldIndicatedAnnual'
    yield_val = metrics.get('dividendYieldIndicatedAnnual', 0.0)
    if yield_val is None:
        return 0.0
    # Finnhub 回傳百分比 (e.g., 0.65 代表 0.65%)，轉為小數
    return round(float(yield_val) / 100.0, 4)


# ---------------------------------------------------------------------------
# Company Profile (標的類型判斷)
# ---------------------------------------------------------------------------
def get_company_profile(symbol: str) -> Dict[str, Any]:
    """
    取得公司/ETF 基本資料 (用於判斷 quoteType)。

    Returns:
        dict with keys like: finnhubIndustry, name, ticker, exchange, etc.
    """
    _rate_limit()
    try:
        client = _get_client()
        data = client.company_profile2(symbol=symbol)
        return data if data else {}
    except Exception as e:
        logger.error(f"[{symbol}] Finnhub company profile 失敗: {e}")
        return {}


def is_etf(symbol: str) -> bool:
    """判斷標的是否為 ETF。"""
    # Finnhub 的 company_profile2 對 ETF 通常回傳空資料或特殊 industry
    # 使用 ETF profile endpoint 替代
    _rate_limit()
    try:
        client = _get_client()
        # 嘗試 ETF profile — 若回傳有效資料則為 ETF
        data = client.etfs_profile(symbol=symbol)
        if data and data.get('name'):
            return True
        return False
    except Exception:
        return False


# ---------------------------------------------------------------------------
# Earnings Calendar (財報日期)
# ---------------------------------------------------------------------------
def get_earnings_calendar(
    symbol: str,
    from_date: Optional[str] = None,
    to_date: Optional[str] = None,
) -> List[Dict[str, Any]]:
    """
    取得財報日曆。

    Args:
        symbol: 標的代號
        from_date: 起始日期 (YYYY-MM-DD)，預設為今天
        to_date: 結束日期 (YYYY-MM-DD)，預設為 90 天後

    Returns:
        list of earnings entries
    """
    _rate_limit()
    try:
        client = _get_client()
        if from_date is None:
            from_date = datetime.now().strftime('%Y-%m-%d')
        if to_date is None:
            to_date = (datetime.now() + timedelta(days=90)).strftime('%Y-%m-%d')

        data = client.earnings_calendar(
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
def get_company_news(
    symbol: str,
    from_date: Optional[str] = None,
    to_date: Optional[str] = None,
    limit: int = 5,
) -> List[Dict[str, Any]]:
    """
    取得公司新聞。

    Args:
        symbol: 標的代號
        from_date: 起始日期 (YYYY-MM-DD)，預設為 7 天前
        to_date: 結束日期 (YYYY-MM-DD)，預設為今天
        limit: 最多回傳筆數

    Returns:
        list of news entries (keys: headline, summary, url, datetime, source, etc.)
    """
    _rate_limit()
    try:
        client = _get_client()
        if to_date is None:
            to_date = datetime.now().strftime('%Y-%m-%d')
        if from_date is None:
            from_date = (datetime.now() - timedelta(days=7)).strftime('%Y-%m-%d')

        data = client.company_news(symbol, _from=from_date, to=to_date)
        if not data:
            return []

        import re
        cleaned_news = []
        seen_headlines = set()
        
        # 建立精確匹配 symbol 的正則表達式 (忽略大小寫，邊界匹配)
        # 避免像是代號 "T" 去匹配到文字中的字母 't'
        symbol_pattern = re.compile(rf'\b{re.escape(symbol)}\b', re.IGNORECASE)
        
        for item in data:
            headline = item.get('headline', '').strip()
            summary = item.get('summary', '').strip()
            
            # 1. 基礎清洗：過濾無標題的新聞
            if not headline:
                continue
                
            # 2. 去重複：同一事件常被多個新聞來源發布，透過轉小寫後的標題去重
            hl_lower = headline.lower()
            if hl_lower in seen_headlines:
                continue
                
            # 3. 相關性清洗：檢查標題或摘要是否確實包含標的代號
            # 避免收集到如「本日市場 50 檔焦點股」這種大雜燴且資訊稀疏的文章
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
# Macro Environment (宏觀環境指標)
# ---------------------------------------------------------------------------
def get_macro_environment() -> Dict[str, float]:
    """
    獲取宏觀環境參數：VIX 與 原油
    """
    try:
        vix_df = get_history_df("^VIX", period="5d")
        oil_df = get_history_df("CL=F", period="5d")
        
        if vix_df.empty or oil_df.empty:
            logger.warning("宏觀數據 (VIX/Oil) 抓取結果為空，使用預設值")
            return {"vix": 18.0, "oil": 75.0, "vix_change": 0.0}

        return {
            "vix": round(float(vix_df['Close'].iloc[-1]), 2),
            "oil": round(float(oil_df['Close'].iloc[-1]), 2),
            "vix_change": round(float(vix_df['Close'].pct_change().iloc[-1]), 4)
        }
    except Exception as e:
        logger.error(f"獲取宏觀環境參數失敗: {e}")
        return {"vix": 18.0, "oil": 75.0, "vix_change": 0.0}
