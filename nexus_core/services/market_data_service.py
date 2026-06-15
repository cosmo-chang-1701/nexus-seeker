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
import random
import math
from datetime import datetime, timedelta
from typing import Optional, List, Dict, Any
from collections import OrderedDict, namedtuple
import gc
import weakref

import finnhub
import pandas as pd
import numpy as np
import yfinance as yf
from aiolimiter import AsyncLimiter

from config import FINNHUB_API_KEY
import database.financials as db_financials

logger = logging.getLogger(__name__)


def _sanitize_ticker(raw: str) -> str:
    """清洗外部輸入的 ticker。

    - 移除前置/後置的 `$`（例如 `$SPCX`）以避免 yfinance HTTP 400。
    - 去除空白並統一大寫，確保 cache key 與下游查詢一致。
    """

    s = (raw or "").strip()
    # 僅移除前置/後置的 `$`，不做更激進的字串重寫以避免破壞如 BRK.B 等格式
    s = s.strip("$")
    return s.upper()


def _to_yfinance_symbol(symbol: str) -> str:
    """將內部 ticker 轉為 yfinance 可接受的格式。"""

    s = _sanitize_ticker(symbol)
    return "^VIX" if s == "VIX" else s


# ---------------------------------------------------------------------------
# 配置與 Rate Limiting (免費方案 60 calls/min)
# ---------------------------------------------------------------------------
# 注意：AsyncLimiter / Semaphore 不建議跨 event loop 重複使用；測試/整合環境可能會建立多個 loop。
# 使用 WeakKeyDictionary 以「loop 物件」為 key，避免 id(loop) 被重用造成 limiter 跨 loop 共享。
_finnhub_controls_by_loop: weakref.WeakKeyDictionary[
    asyncio.AbstractEventLoop, dict[str, Any]
] = weakref.WeakKeyDictionary()

# 429 cooldown 維持全局共享，讓同一個 runtime 內的所有 task 共同避開重試碰撞。
# （單元測試也會 patch 這個變數以驗證行為）
_rate_limit_until = 0.0

# module-level rate limiter semaphore
_limiter: Optional[asyncio.Semaphore] = None

_clients: List[finnhub.Client] = []
_client_idx = 0


def _get_finnhub_controls() -> dict[str, Any]:
    loop = asyncio.get_running_loop()
    controls = _finnhub_controls_by_loop.get(loop)
    if controls is None:
        controls = {
            # 1) 每分鐘 55 次請求（保留緩衝以容納重試）
            "limiter": AsyncLimiter(55, 60),
            # 2) 每秒 8 次請求（抑制突發 burst，避免 Finnhub 以秒級限流回 429）
            "limiter_per_second": AsyncLimiter(8, 1),
            # 3) 併發上限（避免同時間大量 to_thread 造成碰撞與資源抖動）
            "sem": asyncio.Semaphore(3),
        }
        _finnhub_controls_by_loop[loop] = controls
    return controls


def _get_client() -> finnhub.Client:
    """取得或初始化 Finnhub client (輪詢取得)。"""
    global _clients, _client_idx
    if not _clients:
        if not FINNHUB_API_KEY:
            raise RuntimeError("FINNHUB_API_KEY 未設定，請在 .env 中配置")
        keys = [k.strip() for k in FINNHUB_API_KEY.split(",") if k.strip()]
        for key in keys:
            _clients.append(finnhub.Client(api_key=key))
        logger.info(f"Finnhub Client Pool 初始化完成，共載入 {len(_clients)} 個金鑰")

    if not _clients:
        raise RuntimeError("金鑰池為空，請確認 FINNHUB_API_KEY 配置")

    client = _clients[_client_idx]
    if len(_clients) > 1:
        _client_idx = (_client_idx + 1) % len(_clients)
    return client


def _rotate_client() -> finnhub.Client:
    """將金鑰池輪替至下一個金鑰並回傳。"""
    global _clients, _client_idx
    if not _clients:
        _get_client()
    if len(_clients) > 1:
        _client_idx = (_client_idx + 1) % len(_clients)
        logger.info(f"🔄 偵測到頻率限制或重試，輪替 Finnhub 金鑰至索引 {_client_idx}")
    return _clients[_client_idx]


# ---------------------------------------------------------------------------
# Core Async API Call (Thread-safe Wrapper)
# ---------------------------------------------------------------------------
async def _execute_api_call(func, *args, **kwargs) -> Any:
    """執行 Finnhub API 呼叫的異步封裝（生產等級防禦）。

    目標：
    - Rate limiting：同時做「每分鐘」+「每秒」節流，抑制 burst。
    - Concurrency limiting：限制同時間最大併發，避免重試碰撞與 thread 資源抖動。
    - Retries：針對 429/連線錯誤做「指數退避 + 抖動」，讓重試時間錯開。

    注意：limiter 以 event loop 維度維護；429 cooldown 以全局 `_rate_limit_until` 維護。
    """

    global _rate_limit_until, _limiter

    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        loop = None

    if _limiter is None or (loop and getattr(_limiter, "_loop", None) != loop):
        _limiter = asyncio.Semaphore(3)

    controls = _get_finnhub_controls()

    max_retries = 3

    # Introduce Micro-Jitter (Throttling) before entering the semaphore / limiters
    await asyncio.sleep(random.uniform(0.1, 0.3))

    for attempt in range(max_retries + 1):
        # 0) 全局冷卻（先快檢一次，不要讓所有 task 進 limiter 排隊後又卡住）
        now = time.time()
        rate_limit_until = _rate_limit_until
        if now < rate_limit_until:
            wait_time = rate_limit_until - now
            logger.info(f"⏳ 檢測到全局頻率限制中，主動等待 {wait_time:.1f} 秒...")
            await asyncio.sleep(wait_time)

        async with _limiter:
            async with controls["limiter_per_second"]:
                async with controls["limiter"]:
                    # 1) 進入限流鎖後再確認一次（防止排隊期間被其他 task 更新 cooldown）
                    now = time.time()
                    rate_limit_until = _rate_limit_until
                    if now < rate_limit_until:
                        wait_time = rate_limit_until - now
                        logger.info(
                            f"⏳ 限流鎖內確認全局頻率限制，主動等待 {wait_time:.1f} 秒..."
                        )
                        await asyncio.sleep(wait_time)

                    try:
                        # Finnhub SDK 為同步阻塞 I/O，必須在獨立線程中執行
                        return await asyncio.to_thread(func, *args, **kwargs)
                    except Exception as e:
                        error_msg = str(e).lower()
                        is_rate_limit = (
                            "429" in error_msg
                            or "limit reached" in error_msg
                            or "too many requests" in error_msg
                        )
                        is_conn_error = (
                            "connection aborted" in error_msg
                            or "timeout" in error_msg
                            or "remotedisconnected" in error_msg
                            or "temporarily unavailable" in error_msg
                        )

                        if not (is_rate_limit or is_conn_error):
                            raise

                        if attempt >= max_retries:
                            reason = (
                                "429 頻率限制" if is_rate_limit else "連線錯誤/超時"
                            )
                            logger.error(
                                f"🚨 觸發 Finnhub {reason}。已達最大重試次數，放棄呼叫。"
                            )
                            raise

                        # Parse Retry-After or apply exponential backoff fallback
                        if is_rate_limit:
                            retry_after = None
                            if hasattr(e, "response") and e.response is not None:
                                retry_after_hdr = e.response.headers.get(
                                    "Retry-After"
                                ) or e.response.headers.get("retry-after")
                                if retry_after_hdr:
                                    try:
                                        retry_after = float(retry_after_hdr)
                                    except ValueError:
                                        pass
                            if retry_after is not None:
                                delay = retry_after
                            else:
                                delay = (2**attempt) + random.uniform(0.1, 1.0)
                        else:
                            delay = (2**attempt) + random.uniform(0.1, 1.0)

                        # Rotate Finnhub Client for retry if multiple keys are configured
                        if _clients and len(_clients) > 1 and hasattr(func, "__name__"):
                            try:
                                next_client = _rotate_client()
                                func = getattr(next_client, func.__name__)
                            except Exception as ex:
                                logger.warning(f"輪替 Finnhub 實例失敗: {ex}")

                        if is_rate_limit:
                            # 使用 max() 保留最長冷卻時間，避免被較短 delay 覆蓋
                            _rate_limit_until = max(
                                _rate_limit_until, time.time() + delay
                            )

                        reason = "429 頻率限制" if is_rate_limit else "連線錯誤/超時"
                        logger.warning(
                            f"🚨 觸發 Finnhub {reason}。將於 {delay:.1f} 秒後重試 (次數: {attempt + 1}/{max_retries})..."
                        )
                        await asyncio.sleep(delay)
                        continue


# ---------------------------------------------------------------------------
# Quote (即時報價)
# ---------------------------------------------------------------------------
async def _safe_yf_history(
    ticker: yf.Ticker,
    *,
    period: str,
    interval: Optional[str] = None,
) -> Optional[pd.DataFrame]:
    """安全包裝 yfinance history。

    - 捕捉 yfinance 的 HTTP 400 / delisted / 空資料等狀況
    - 若回傳空 DataFrame，統一回傳 None（呼叫端再做 fallback/降級）
    """

    try:
        if interval is None:
            df = await asyncio.to_thread(ticker.history, period=period)
        else:
            df = await asyncio.to_thread(
                ticker.history, period=period, interval=interval
            )

        if df is None or getattr(df, "empty", True):
            return None
        return df
    except Exception as e:
        logger.warning(f"yfinance history 失敗: {e}")
        return None


async def get_yfinance_quote(symbol: str) -> Dict[str, Any]:
    """使用 yfinance 取得即時報價，並轉換格式與 Finnhub 相容。

    防禦性處理：
    - 自動清洗 ticker（移除 `$` 與空白），避免 yfinance HTTP 400。
    - 若回傳為空則記錄 warning 並回傳空 dict，避免中斷批次任務。
    """

    yf_symbol = _to_yfinance_symbol(symbol)
    try:
        ticker = yf.Ticker(yf_symbol)
        # 抓取最近 2 天資料以計算昨日收盤 (pc)
        df = await _safe_yf_history(ticker, period="2d")
        if df is None:
            logger.warning(f"[{yf_symbol}] yfinance quote 回傳資料為空")
            return {}

        latest = df.iloc[-1]
        prev_close = df.iloc[-2]["Close"] if len(df) > 1 else latest["Open"]
        current_price = latest["Close"]

        change = current_price - prev_close
        pct_change = (change / prev_close) * 100 if prev_close != 0 else 0.0

        return {
            "c": round(float(current_price), 2),
            "d": round(float(change), 2),
            "dp": round(float(pct_change), 4),
            "h": round(float(latest["High"]), 2),
            "l": round(float(latest["Low"]), 2),
            "o": round(float(latest["Open"]), 2),
            "pc": round(float(prev_close), 2),
            "t": int(df.index[-1].timestamp()),
        }
    except Exception as e:
        logger.error(f"[{yf_symbol}] yfinance quote 失敗: {e}")
        return {}


async def get_quote(symbol: str) -> Dict[str, Any]:
    """取得即時報價 (非同步)。對於指數型標的，強制轉向 yfinance。"""
    symbol = _sanitize_ticker(symbol)
    now = time.time()
    if symbol in _quote_cache:
        val, expiry = _quote_cache[symbol]
        if now < expiry:
            return val

    async def _fetch():
        if symbol.startswith("^") or symbol == "VIX":
            return await get_yfinance_quote(symbol)

        client = _get_client()
        try:
            data = await _execute_api_call(client.quote, symbol)
            if data and data.get("c", 0) > 0:
                return data

            # 若 Finnhub 回傳無效或報權限錯誤 (c=0 有可能是權限問題或標的不存在)
            # 嘗試作為 fallback 轉向 yfinance
            logger.warning(f"[{symbol}] Finnhub quote 無效，嘗試 yfinance fallback")
            return await get_yfinance_quote(symbol)
        except Exception as e:
            # 如果是明確的權限錯誤，也轉向 yfinance
            error_msg = str(e).lower()
            if "subscription required" in error_msg or "market data" in error_msg:
                logger.info(f"[{symbol}] Finnhub 權限受限，強制轉向 yfinance")
                return await get_yfinance_quote(symbol)

            logger.error(f"[{symbol}] Finnhub quote 失敗: {e}")
            return {}

    res = await _fetch()
    if res and res.get("c", 0) > 0:
        _quote_cache[symbol] = (res, now + _QUOTE_CACHE_TTL)
    return res


async def validate_symbol(symbol: str) -> bool:
    """驗證標的代號是否有效 (具備即時報價、本地資料庫對比及格式後備機制)。"""
    if not symbol:
        return False

    symbol = symbol.strip().upper()

    # 1. 基礎格式驗證：代號長度不合理或包含非法字元直接過濾
    import re

    if not re.match(r"^[\^A-Z0-9.-]{1,10}$", symbol):
        return False

    # 2. 嘗試獲取即時報價，若有價格大於 0 則必為有效標的
    try:
        quote = await get_quote(symbol)
        if quote and quote.get("c", 0) > 0:
            return True
    except Exception as e:
        logger.warning(f"validate_symbol 獲取報價異常: {e}")

    # 3. 後備機制 A：當 API 因限流、盤前/週末或網路波動而失效時，比對本地資料庫中是否已有該標的之運作紀錄
    import sqlite3
    import config

    try:
        with sqlite3.connect(config.DB_NAME) as conn:
            cursor = conn.cursor()

            # 3.1 檢查 market_cache
            try:
                cursor.execute(
                    "SELECT 1 FROM market_cache WHERE UPPER(symbol) = ? LIMIT 1",
                    (symbol,),
                )
                if cursor.fetchone():
                    logger.info(
                        f"[{symbol}] 報價失敗，但於本地資料庫 market_cache 中尋獲紀錄，判定為有效代號"
                    )
                    return True
            except sqlite3.OperationalError:
                pass

            # 3.2 檢查 watchlist
            try:
                cursor.execute(
                    "SELECT 1 FROM watchlist WHERE UPPER(symbol) = ? LIMIT 1", (symbol,)
                )
                if cursor.fetchone():
                    logger.info(
                        f"[{symbol}] 報價失敗，但於本地資料庫 watchlist 中尋獲紀錄，判定為有效代號"
                    )
                    return True
            except sqlite3.OperationalError:
                pass

            # 3.3 檢查 portfolio
            try:
                cursor.execute(
                    "SELECT 1 FROM portfolio WHERE UPPER(symbol) = ? LIMIT 1", (symbol,)
                )
                if cursor.fetchone():
                    logger.info(
                        f"[{symbol}] 報價失敗，但於本地資料庫 portfolio 中尋獲紀錄，判定為有效代號"
                    )
                    return True
            except sqlite3.OperationalError:
                pass

            # 3.4 檢查 active_orders
            try:
                cursor.execute(
                    "SELECT 1 FROM active_orders WHERE UPPER(symbol) = ? LIMIT 1",
                    (symbol,),
                )
                if cursor.fetchone():
                    logger.info(
                        f"[{symbol}] 報價失敗，但於本地資料庫 active_orders 中尋獲紀錄，判定為有效代號"
                    )
                    return True
            except sqlite3.OperationalError:
                pass

            # 3.5 檢查 historical_iv
            try:
                cursor.execute(
                    "SELECT 1 FROM historical_iv WHERE UPPER(symbol) = ? LIMIT 1",
                    (symbol,),
                )
                if cursor.fetchone():
                    logger.info(
                        f"[{symbol}] 報價失敗，但於本地資料庫 historical_iv 中尋獲紀錄，判定為有效代號"
                    )
                    return True
            except sqlite3.OperationalError:
                pass

    except Exception as e:
        logger.error(f"validate_symbol 資料庫後備驗證失敗: {e}")

    # 4. 後備機制 B：若 API 與資料庫皆查無紀錄（如新添加標的但遇到 API 故障），若符合典型美股/指數格式則放行容錯
    clean_sym = symbol.lstrip("^")
    if re.match(r"^[A-Z0-9.-]{1,6}$", clean_sym):
        logger.warning(
            f"[{symbol}] 報價失敗且資料庫無紀錄，但符合典型美股/指數格式，寬鬆放行"
        )
        return True

    return False


async def batch_get_quotes(symbols: List[str]) -> Dict[str, Dict[str, Any]]:
    """批次取得多檔標的的即時報價。

    防禦性設計：
    - 先做 ticker 清洗（移除 `$`/空白並大寫），避免 yfinance/Finnhub 請求格式錯誤。
    - 使用批次級 ``asyncio.Semaphore(3)`` 限制同時在途的報價請求數量，
      避免大量 watchlist 標的同時湧入下游 limiter 佇列而觸發 Finnhub 429。
      （每個 ``get_quote()`` 仍有自己的 per-call rate limiter，此為外層保護層。）
    """

    clean_symbols = [_sanitize_ticker(s) for s in symbols if s]
    batch_sem = asyncio.Semaphore(3)

    async def _throttled_quote(sym: str) -> Dict[str, Any]:
        async with batch_sem:
            return await get_quote(sym)

    tasks = [_throttled_quote(sym) for sym in clean_symbols]
    quotes = await asyncio.gather(*tasks)
    return {sym: q for sym, q in zip(clean_symbols, quotes) if q}


# ---------------------------------------------------------------------------
# 歷史數據與指標 (yfinance)
# ---------------------------------------------------------------------------
async def get_history_df(
    symbol: str, period: str = "1y", interval: str = "1d"
) -> pd.DataFrame:
    """
    使用 yfinance 抓取歷史 K 線 (異步化，支援 4 小時快取與 Copy 隔離)。
    """
    symbol = _to_yfinance_symbol(symbol)
    cache_key = (symbol, period, interval)
    now = time.time()

    if cache_key in _history_cache:
        cached_df, expiry = _history_cache[cache_key]
        if now < expiry:
            return cached_df.copy()

    try:
        ticker = yf.Ticker(symbol)
        df = await _safe_yf_history(ticker, period=period, interval=interval)

        if df is None:
            logger.warning(
                f"[{symbol}] yfinance 歷史數據為空 (period={period}, interval={interval})"
            )
            return pd.DataFrame()

        df.index.name = "Date"
        if df.index.tz is not None:
            df.index = df.index.tz_localize(None)

        result_df = df[["Open", "High", "Low", "Close", "Volume"]]
        _history_cache[cache_key] = (result_df.copy(), now + _HISTORY_CACHE_TTL)
        return result_df
    except Exception as e:
        logger.error(f"[{symbol}] yfinance 抓取失敗: {e}")
        return pd.DataFrame()


async def get_spy_history_df(
    period: str = "1y", interval: str = "1d", retries: int = 3
) -> pd.DataFrame:
    """取得 SPY 基準歷史資料，針對暫時性鎖衝突進行重試。"""
    for attempt in range(retries):
        df = await get_history_df("SPY", period=period, interval=interval)
        if not df.empty:
            return df
        await asyncio.sleep(0.4 * (attempt + 1))

    logger.error(f"[SPY] 重試 {retries} 次後仍無法取得歷史資料")
    return pd.DataFrame()


async def get_stock_splits(symbol: str) -> pd.Series:
    """取得標的的拆股歷史資料。"""
    symbol = _sanitize_ticker(symbol)
    try:
        ticker = yf.Ticker(symbol)
        splits = await asyncio.to_thread(lambda: ticker.splits)
        if splits is None:
            return pd.Series(dtype=float)
        return splits
    except Exception as e:
        logger.error(f"[{symbol}] yfinance 獲取拆股歷史失敗: {e}")
        return pd.Series(dtype=float)


OptionChainData = namedtuple("OptionChainData", ["calls", "puts", "underlying"])


async def get_all_option_expiries(symbol: str) -> List[str]:
    """取得該標的所有可用的期權到期日 (支援 12 小時快取)。"""
    symbol = _sanitize_ticker(symbol)
    now = time.time()
    if symbol in _option_expiries_cache:
        cached_val, expiry = _option_expiries_cache[symbol]
        if now < expiry:
            return list(cached_val)

    try:
        ticker = yf.Ticker(symbol)
        expiries = await asyncio.to_thread(lambda: ticker.options)
        res = list(expiries)
        if res:
            _option_expiries_cache[symbol] = (
                list(res),
                now + _OPTION_EXPIRIES_CACHE_TTL,
            )
        return res
    except Exception as e:
        logger.error(f"[{symbol}] 獲取期權到期日失敗: {e}")
        return []


async def get_option_chain(symbol: str, expiry: str) -> Optional[Any]:
    """取得指定到期日的期權鏈 (支援 40 分鐘快取與 Copy 隔離)。"""
    symbol = _sanitize_ticker(symbol)
    cache_key = (symbol, expiry)
    now = time.time()

    if cache_key in _option_chain_cache:
        cached_val, expiry_time = _option_chain_cache[cache_key]
        if now < expiry_time:
            calls_copy = (
                cached_val.calls.copy() if cached_val.calls is not None else None
            )
            puts_copy = cached_val.puts.copy() if cached_val.puts is not None else None
            underlying_copy = (
                cached_val.underlying.copy()
                if hasattr(cached_val.underlying, "copy")
                else cached_val.underlying
            )
            return OptionChainData(
                calls=calls_copy, puts=puts_copy, underlying=underlying_copy
            )

    try:
        ticker = yf.Ticker(symbol)
        chain = await asyncio.to_thread(ticker.option_chain, expiry)
        if chain is not None:
            calls_copy = chain.calls.copy() if chain.calls is not None else None
            puts_copy = chain.puts.copy() if chain.puts is not None else None
            underlying_copy = (
                chain.underlying.copy()
                if hasattr(chain.underlying, "copy")
                else chain.underlying
            )
            cached_entry = OptionChainData(
                calls=calls_copy, puts=puts_copy, underlying=underlying_copy
            )
            _option_chain_cache[cache_key] = (
                cached_entry,
                now + _OPTION_CHAIN_CACHE_TTL,
            )
            return OptionChainData(
                calls=calls_copy.copy() if calls_copy is not None else None,
                puts=puts_copy.copy() if puts_copy is not None else None,
                underlying=underlying_copy.copy()
                if hasattr(underlying_copy, "copy")
                else underlying_copy,
            )
        return None
    except Exception as e:
        logger.error(f"[{symbol}] 獲取期權鏈失敗 (expiry={expiry}): {e}")
        return None


# 限制快取大小以節省記憶體 (1GB RAM VPS 優化)
MAX_CACHE_SIZE = 500


class BoundedCache(OrderedDict):
    """具備容量上限的快取 (LRU 邏輯)。"""

    def __init__(self, max_size=MAX_CACHE_SIZE):
        super().__init__()
        self.max_size = max_size

    def __getitem__(self, key):
        value = super().__getitem__(key)
        self.move_to_end(key)
        return value

    def __setitem__(self, key, value):
        if key in self:
            self.move_to_end(key)
        super().__setitem__(key, value)
        if len(self) > self.max_size:
            self.popitem(last=False)


# ---------------------------------------------------------------------------
# SMA 記憶體快取設定
# ---------------------------------------------------------------------------
_sma_cache = BoundedCache(max_size=MAX_CACHE_SIZE)
_SMA_CACHE_TTL = 3600  # 1 小時 (1GB VPS 優化)


# ---------------------------------------------------------------------------
# 即時報價與基本面資料快取設定
# ---------------------------------------------------------------------------
_quote_cache = BoundedCache(max_size=MAX_CACHE_SIZE)
_QUOTE_CACHE_TTL = 15  # 15 秒，避免在同一次掃描中心跳訊號重複對相同標的進行即時報價呼叫

_profile_cache = BoundedCache(max_size=MAX_CACHE_SIZE)
_PROFILE_CACHE_TTL = 86400  # 24 小時，公司 Profile 通常是靜態的

_etf_cache = BoundedCache(max_size=MAX_CACHE_SIZE)
_ETF_CACHE_TTL = 86400  # 24 小時，ETF 屬性通常是靜態的

# ---------------------------------------------------------------------------
# 歷史 K 線數據快取設定 (6 小時，避開盤中大量重複 API 查詢)
# ---------------------------------------------------------------------------
_history_cache = BoundedCache(max_size=MAX_CACHE_SIZE)
_HISTORY_CACHE_TTL = 21600  # 6 小時

# ---------------------------------------------------------------------------
# 期權到期日與期權鏈快取設定 (避開盤中重複的 yfinance 查詢)
# ---------------------------------------------------------------------------
_option_expiries_cache = BoundedCache(max_size=MAX_CACHE_SIZE)
_OPTION_EXPIRIES_CACHE_TTL = 43200  # 12 小時

_option_chain_cache = BoundedCache(max_size=MAX_CACHE_SIZE)
_OPTION_CHAIN_CACHE_TTL = 1200  # 20 分鐘


def clear_quote_cache():
    _quote_cache.clear()
    logger.info("Clarified quote cache")


def clear_profile_cache():
    _profile_cache.clear()
    logger.info("Clarified profile cache")


def clear_etf_cache():
    _etf_cache.clear()
    logger.info("Clarified ETF cache")


def clear_history_cache():
    _history_cache.clear()
    logger.info("Clarified history cache")


def clear_options_cache():
    _option_expiries_cache.clear()
    _option_chain_cache.clear()
    logger.info("Clarified options cache")


async def get_sma(symbol: str, window: int = 200) -> Optional[float]:
    """計算簡單移動平均線 (SMA)。"""
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

        sma_series = df["Close"].rolling(window=window).mean()
        current_sma = round(float(sma_series.iloc[-1]), 4)

        if not pd.isna(current_sma):
            _sma_cache[cache_key] = (current_sma, current_time + _SMA_CACHE_TTL)

        return current_sma if not pd.isna(current_sma) else None
    except Exception as e:
        logger.error(f"[{symbol}] 計算 SMA{window} 失敗: {e}")
        return None


def clear_sma_cache():
    _sma_cache.clear()
    logger.info("Clarified SMA cache")


# ---------------------------------------------------------------------------
# EMA 記憶體快取設定
# ---------------------------------------------------------------------------
_ema_cache = BoundedCache(max_size=MAX_CACHE_SIZE)
_EMA_CACHE_TTL = 3600  # 1 小時 (1GB VPS 優化)


async def get_ema(symbol: str, window: int = 21) -> Optional[float]:
    """計算指數移動平均線 (EMA)。"""
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

        ema_series = df["Close"].ewm(span=window, adjust=False).mean()
        current_ema = round(float(ema_series.iloc[-1]), 4)

        if not np.isnan(current_ema):
            _ema_cache[cache_key] = (current_ema, now + _EMA_CACHE_TTL)
        return current_ema if not np.isnan(current_ema) else None
    except Exception as e:
        logger.error(f"[{symbol}] EMA{window} 計算失敗: {e}")
        return None


def clear_ema_cache():
    _ema_cache.clear()
    logger.info("Clarified EMA cache")


def run_garbage_collection():
    """手動觸發垃圾回收 (用於大規模掃描後)。"""
    gc.collect()
    logger.info("🧹 [系統優化] 已手動執行垃圾回收機制。")


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
        metrics = data.get("metric", {}) if data else {}

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
            return val

    client = _get_client()
    try:
        data = await _execute_api_call(client.company_profile2, symbol=symbol)
        res = data if data else {}
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
            return val

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
        return data.get("economicCalendar", []) if data else []
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
        return earnings
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
    try:
        vix_task = get_history_df("^VIX", period="5d")
        vix3m_task = get_history_df("^VIX3M", period="5d")
        vix_df, vix3m_df = await asyncio.gather(vix_task, vix3m_task)

        if vix_df.empty or vix3m_df.empty:
            return {"vts_ratio": 1.0, "vts_state": "UNKNOWN"}

        vix_close = float(vix_df["Close"].iloc[-1])
        vix3m_close = float(vix3m_df["Close"].iloc[-1])

        if vix3m_close > 0:
            vts_ratio = round(vix_close / vix3m_close, 3)
        else:
            vts_ratio = 1.0

        state = "Backwardation" if vts_ratio >= 1.0 else "Contango"
        return {
            "vts_ratio": vts_ratio,
            "vts_state": state,
            "vix_front": vix_close,
            "vix_back": vix3m_close,
        }
    except Exception as e:
        logger.error(f"VIX 期限結構計算失敗: {e}")
        return {"vts_ratio": 1.0, "vts_state": "UNKNOWN"}


async def get_vix_zscores() -> Dict[str, float]:
    """取得 VIX 30天與60天 Z-Score"""
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
            async def _async_revalidate_max_pain():
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
