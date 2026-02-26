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
from datetime import datetime, timedelta
from typing import Optional, List, Dict, Any

import yfinance as yf
import pandas as pd
import logging

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
        return data[:limit] if data else []
    except Exception as e:
        logger.error(f"[{symbol}] Finnhub company news 失敗: {e}")
        return []
