"""
Finnhub Service — 集中式 Finnhub API client wrapper (Async Optimized)。

所有對 Finnhub REST API 的呼叫統一經過此模組，確保：
1. API Key 集中管理
2. Rate limiting（免費方案 60 calls/min, 使用 aiolimiter 控制）
3. 錯誤處理與 fallback
4. 回傳格式與既有程式碼相容（pandas DataFrame）
"""

from typing import Any
import asyncio
import contextvars
import functools
import logging
import time
import random
import math
from contextlib import contextmanager
from datetime import datetime, timedelta
from typing import Optional, List, Dict, cast
from collections import namedtuple
import gc
import weakref

import finnhub
import pandas as pd
import numpy as np
import yfinance as yf
from aiolimiter import AsyncLimiter

from config import FINNHUB_API_KEY
from market_time import ny_tz
import database.financials as db_financials
from services.bounded_cache import BoundedCache  # noqa: F401 (re-exported below)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# 互動請求優先權標記（Context-local）
# ---------------------------------------------------------------------------
# 用於區分「使用者互動指令」（如 /x）與「背景排程」（心跳/掃描）對 yfinance /
# Finnhub 的呼叫來源，讓下方的限流池能替互動請求保留獨立的併發與每分鐘額度，
# 避免背景任務把共用額度佔滿、導致互動指令長時間排隊卡住。
# 透過 contextvars 傳遞：asyncio.gather / asyncio.create_task 產生的子協程會
# 自動繼承呼叫當下的 context，不需要逐層手動傳遞旗標。
_is_interactive_request: contextvars.ContextVar[bool] = contextvars.ContextVar(
    "is_interactive_request", default=False
)


@contextmanager
def mark_interactive_request() -> Any:
    """標記目前 context 內所有下游 API 呼叫為使用者互動來源（例如 /x 指令）。"""
    token = _is_interactive_request.set(True)
    try:
        yield
    finally:
        _is_interactive_request.reset(token)


def interactive(func: Any) -> Any:
    """裝飾器版本的 `mark_interactive_request`：標記被裝飾的 async 方法整個執行
    期間（含其內部 asyncio.gather/create_task 產生的子協程）為互動請求來源，
    無需在呼叫端或函式內部手動包 `with` 區塊。用於 /x 指令的入口方法。"""

    @functools.wraps(func)
    async def wrapper(*args: Any, **kwargs: Any) -> Any:
        with mark_interactive_request():
            return await func(*args, **kwargs)

    return wrapper


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

_client: Optional[finnhub.Client] = None


def _get_finnhub_controls() -> dict[str, Any]:
    loop = asyncio.get_running_loop()
    controls = _finnhub_controls_by_loop.get(loop)
    if controls is None:
        controls = {
            # 1) 全局單一母令牌桶 (硬上限 50 次/60 秒，保留 10 次作為防護冗餘緩衝)
            #    所有 Finnhub 呼叫均須先通過此桶，徹底避免背景與互動獨立計數導致突破 60 次/分
            "limiter_global": AsyncLimiter(50, 60),
            # 2) 背景任務次級配額上限 (最多 12 次/60 秒)，為互動指令預留至少 38+ 次配額
            "limiter_background": AsyncLimiter(12, 60),
            # 3) 每秒最大突發 (Burst) 上限降至 3 次，防止微秒級 socket 洪峰打穿 Finnhub WAF
            "limiter_per_second": AsyncLimiter(3, 1),
            # 4) 併發上限：背景 2 個 + 互動 3 個，平滑請求分發
            "sem_background": asyncio.Semaphore(2),
            "sem_interactive": asyncio.Semaphore(3),
        }
        _finnhub_controls_by_loop[loop] = controls
    return controls


# ---------------------------------------------------------------------------
# yfinance 節流（與 Finnhub 對稱：yfinance 沒有官方 rate limit API，
# 但無節流會導致 Yahoo 端 IP 封鎖，故套用保守的 limiter + 併發上限）
# ---------------------------------------------------------------------------
_yfinance_controls_by_loop: weakref.WeakKeyDictionary[
    asyncio.AbstractEventLoop, dict[str, Any]
] = weakref.WeakKeyDictionary()


# ---------------------------------------------------------------------------
# Edge Scraper (TUNNEL_URL) HTTP 連線池與 Keep-Alive
# ---------------------------------------------------------------------------
_edge_clients_by_loop: weakref.WeakKeyDictionary[asyncio.AbstractEventLoop, Any] = (
    weakref.WeakKeyDictionary()
)


class _EdgeClientContext:
    def __init__(self, client: Any):
        self._client = client

    async def __aenter__(self) -> Any:
        is_real_httpx = type(self._client).__name__ == "AsyncClient" and getattr(
            type(self._client), "__module__", ""
        ).startswith("httpx")
        if hasattr(self._client, "__aenter__") and not is_real_httpx:
            return await self._client.__aenter__()
        return self._client

    async def __aexit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> Any:
        is_real_httpx = type(self._client).__name__ == "AsyncClient" and getattr(
            type(self._client), "__module__", ""
        ).startswith("httpx")
        if hasattr(self._client, "__aexit__") and not is_real_httpx:
            return await self._client.__aexit__(exc_type, exc_val, exc_tb)
        return False


def get_edge_client() -> Any:
    """取得當前 event loop 的共用 Edge Scraper HTTP 連線池 (Keep-Alive)。"""
    import httpx

    loop = asyncio.get_running_loop()
    client = _edge_clients_by_loop.get(loop)
    if client is None or getattr(client, "is_closed", False):
        client = httpx.AsyncClient(
            timeout=httpx.Timeout(20.0, connect=5.0),
            limits=httpx.Limits(max_keepalive_connections=20, max_connections=50),
            follow_redirects=True,
        )
        _edge_clients_by_loop[loop] = client
    return _EdgeClientContext(client)


def _get_yfinance_controls() -> dict[str, Any]:
    loop = asyncio.get_running_loop()
    controls = _yfinance_controls_by_loop.get(loop)
    if controls is None:
        controls = {
            "limiter_background": AsyncLimiter(20, 60),
            "limiter_interactive": AsyncLimiter(30, 60),
            "sem_background": asyncio.Semaphore(2),
            "sem_interactive": asyncio.Semaphore(5),
        }
        _yfinance_controls_by_loop[loop] = controls
    return controls


async def call_yf(func: Any, *args: Any, **kwargs: Any) -> Any:
    """統一節流包裝：所有對 yfinance 的 blocking 呼叫都應經過這裡。
    依 `_is_interactive_request` context 挑選互動或背景限流池。"""
    controls = _get_yfinance_controls()
    if _is_interactive_request.get():
        limiter, sem = controls["limiter_interactive"], controls["sem_interactive"]
    else:
        limiter, sem = controls["limiter_background"], controls["sem_background"]
    async with limiter:
        async with sem:
            return await asyncio.to_thread(func, *args, **kwargs)


def _get_client() -> finnhub.Client:
    """取得或初始化 Finnhub client。"""
    global _client
    if _client is None:
        if not FINNHUB_API_KEY:
            raise RuntimeError("FINNHUB_API_KEY 未設定，請在 .env 中配置")
        keys = [k.strip() for k in FINNHUB_API_KEY.split(",") if k.strip()]
        _client = finnhub.Client(api_key=keys[0])
        if len(keys) > 1:
            logger.warning(
                "檢測到多組 FINNHUB_API_KEY，為避免被封鎖，系統已強制僅使用第一組金鑰。"
            )
        logger.info("Finnhub Client 初始化完成")

    return _client


def is_finnhub_rate_limited() -> bool:
    """檢查 Finnhub 是否正處於全域頻率限制冷卻中"""
    return time.time() < _rate_limit_until


# ---------------------------------------------------------------------------
# Core Async API Call (Thread-safe Wrapper)
# ---------------------------------------------------------------------------
async def _execute_api_call(func: Any, *args, **kwargs) -> Any:  # type: ignore
    """執行 Finnhub API 呼叫的異步封裝（生產等級防禦）。

    目標：
    - Rate limiting：全局單一母令牌桶 (50/60s) + 每秒微步流控 (3/1s)，徹底抑制 burst。
    - Concurrency limiting：限制同時間最大併發，避免重試碰撞與 thread 資源抖動。
    - Pacing & Jitter：互動請求加入微步流控 (30~60ms)，冷卻甦醒加入隨機抖動避免群湧 (Thundering Herd)。
    - Fast Circuit-Breaker：互動請求在 429 或冷卻期內立即快速熔斷並拋出例外，供上游無縫降級。

    注意：limiter 以 event loop 維度維護；429 cooldown 以全局 `_rate_limit_until` 維護。
    """

    global _rate_limit_until

    controls = _get_finnhub_controls()
    is_interactive = _is_interactive_request.get()
    sem = controls["sem_interactive"] if is_interactive else controls["sem_background"]

    max_retries = 3

    # 1. 微步流控 (Micro-Pacing)：平滑請求間隔，消除同一毫秒內的 socket 併發洪峰
    if is_interactive:
        await asyncio.sleep(random.uniform(0.03, 0.06))
    else:
        await asyncio.sleep(random.uniform(0.10, 0.25))

    for attempt in range(max_retries + 1):
        # 0) 全局冷卻（先快檢一次，不要讓所有 task 進 limiter 排隊後又卡住）
        now = time.time()
        rate_limit_until = _rate_limit_until
        if now < rate_limit_until:
            if is_interactive:
                logger.warning(
                    "🚨 互動請求檢測到 Finnhub 正處於限流冷卻中，快速熔斷轉向 fallback"
                )
                raise Exception("Finnhub rate limited, fast-circuit to fallback")
            wait_time = (rate_limit_until - now) + random.uniform(0.1, 0.5)
            logger.info(f"⏳ 檢測到全局頻率限制中，主動錯峰等待 {wait_time:.1f} 秒...")
            await asyncio.sleep(wait_time)

        async with sem:
            async with controls["limiter_per_second"]:
                async with controls["limiter_global"]:
                    # 若為背景任務，須額外遵循背景次級配額 (12/60s)
                    if not is_interactive:
                        async with controls["limiter_background"]:
                            # 1) 進入限流鎖後再確認一次（防止排隊期間被其他 task 更新 cooldown）
                            now = time.time()
                            rate_limit_until = _rate_limit_until
                            if now < rate_limit_until:
                                wait_time = (rate_limit_until - now) + random.uniform(
                                    0.1, 0.5
                                )
                                logger.info(
                                    f"⏳ 限流鎖內確認全局頻率限制，主動錯峰等待 {wait_time:.1f} 秒..."
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
                                        "429 頻率限制"
                                        if is_rate_limit
                                        else "連線錯誤/超時"
                                    )
                                    logger.error(
                                        f"🚨 觸發 Finnhub {reason}。已達最大重試次數，放棄呼叫。"
                                    )
                                    raise

                                # Parse Retry-After or apply exponential backoff fallback
                                if is_rate_limit:
                                    retry_after = None
                                    if (
                                        hasattr(e, "response")
                                        and e.response is not None
                                    ):
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
                                        delay = (3**attempt) * 2 + random.uniform(
                                            1.0, 3.0
                                        )
                                else:
                                    delay = (2**attempt) + random.uniform(0.5, 1.5)

                                if is_rate_limit:
                                    # 使用 max() 保留最長冷卻時間，避免被較短 delay 覆蓋
                                    _rate_limit_until = max(
                                        _rate_limit_until, time.time() + delay
                                    )

                                reason = (
                                    "429 頻率限制" if is_rate_limit else "連線錯誤/超時"
                                )
                                jittered_delay = delay + random.uniform(0.1, 0.5)
                                logger.warning(
                                    f"🚨 觸發 Finnhub {reason}。將於 {jittered_delay:.1f} 秒後重試 (次數: {attempt + 1}/{max_retries})..."
                                )
                                await asyncio.sleep(jittered_delay)
                                continue
                    else:
                        # 互動請求路徑
                        now = time.time()
                        rate_limit_until = _rate_limit_until
                        if now < rate_limit_until:
                            logger.warning(
                                "🚨 限流鎖內確認 Finnhub 正處於冷卻中，互動請求快速熔斷"
                            )
                            raise Exception(
                                "Finnhub rate limited, fast-circuit to fallback"
                            )

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
                                    delay = (3**attempt) * 2 + random.uniform(1.0, 3.0)
                            else:
                                delay = (2**attempt) + random.uniform(0.5, 1.5)

                            if is_rate_limit:
                                _rate_limit_until = max(
                                    _rate_limit_until, time.time() + delay
                                )

                            # 針對使用者互動請求（如 /x 指令），若遇 429 直接快速熔斷拋出，
                            # 讓呼叫端立即無縫降級至 yfinance fallback，避免在互動路徑中 sleep 阻塞。
                            if is_rate_limit:
                                logger.warning(
                                    f"🚨 互動請求觸發 Finnhub 429 限流，立即快速熔斷並轉向 fallback (冷卻至 {delay:.1f}s 後)"
                                )
                                raise

                            reason = "連線錯誤/超時"
                            logger.warning(
                                f"🚨 觸發 Finnhub {reason}。將於 {delay:.1f} 秒後重試 (次數: {attempt + 1}/{max_retries})..."
                            )
                            await asyncio.sleep(delay)
                            continue


# ---------------------------------------------------------------------------
# Quote (即時報價)
# ---------------------------------------------------------------------------
async def _fetch_history_via_edge(
    symbol: str, *, period: str, interval: Optional[str] = None
) -> Optional[pd.DataFrame]:
    """優先透過 Edge 節點即時抓取 K 線。未設定 TUNNEL_URL 或抓取失敗/空值時回傳 None。"""
    from config import TUNNEL_URL
    import urllib.parse

    base_url = (
        getattr(TUNNEL_URL, "rstrip", lambda x: TUNNEL_URL)("/") if TUNNEL_URL else ""
    )
    if not base_url:
        return None

    try:
        req_url = f"{base_url}/api/v1/scrape/yf/history/{urllib.parse.quote(str(symbol))}?period={period}"
        if interval:
            req_url += f"&interval={interval}"

        async with get_edge_client() as client:
            res = await client.get(req_url)
            if res.status_code == 200:
                data = res.json()
                if data.get("status") == "success":
                    records = data.get("data", [])
                    if records:
                        df_edge = pd.DataFrame(records)
                        if "Date" in df_edge.columns:
                            df_edge["Date"] = pd.to_datetime(
                                df_edge["Date"], utc=True
                            ).dt.tz_convert(ny_tz)
                            df_edge.set_index("Date", inplace=True)
                        elif "Datetime" in df_edge.columns:
                            df_edge["Datetime"] = pd.to_datetime(
                                df_edge["Datetime"], utc=True
                            ).dt.tz_convert(ny_tz)
                            df_edge.set_index("Datetime", inplace=True)
                        logger.info(f"[{symbol}] Edge 節點成功抓取 K 線")
                        return df_edge
                    else:
                        # Edge 節點已明確確認查無數據 (如標的下市)，回傳空 DataFrame 避免無謂本地重試
                        return pd.DataFrame()
    except Exception as ex:
        logger.warning(f"[{symbol}] Edge 節點即時抓取 K 線失敗: {ex}")

    return None


async def _direct_yf_history(
    ticker: yf.Ticker, *, period: str, interval: Optional[str] = None
) -> Optional[pd.DataFrame]:
    """nexus_core 直連 yfinance 抓取 K 線（降級方案），加入 repair 容錯。"""
    df = None
    try:
        kwargs: dict[str, Any] = {
            "period": period,
            "auto_adjust": True,
            "repair": True,
        }
        if interval is not None:
            kwargs["interval"] = interval

        df = await call_yf(ticker.history, **kwargs)
    except Exception as e:
        logger.warning(
            f"yfinance history (repair=True) 失敗: {e}，嘗試降級使用 repair=False 重試..."
        )
        try:
            kwargs_fallback: dict[str, Any] = {
                "period": period,
                "auto_adjust": True,
                "repair": False,
            }
            if interval is not None:
                kwargs_fallback["interval"] = interval
            df = await call_yf(ticker.history, **kwargs_fallback)
        except Exception as e2:
            logger.warning(f"yfinance history 直接呼叫失敗: {e2}")

    if df is None or getattr(df, "empty", True):
        return None
    return df


async def _safe_yf_history(
    ticker: yf.Ticker,
    *,
    period: str,
    interval: Optional[str] = None,
) -> Optional[pd.DataFrame]:
    """安全包裝 yfinance history：優先透過 Edge 節點抓取，
    nexus_core 直連 yfinance（含 repair 容錯）僅作為 Edge 不可用時的降級方案。
    """

    symbol = getattr(ticker, "ticker", "")
    if symbol:
        df_edge = await _fetch_history_via_edge(
            symbol, period=period, interval=interval
        )
        if df_edge is not None:
            if not df_edge.empty:
                return df_edge
            # Edge 節點已確認無數據，直接返回 None，不再走本地直連
            return None
        logger.info(f"[{symbol}] 降級改用本地 yfinance 直連抓取 K 線...")

    return await _direct_yf_history(ticker, period=period, interval=interval)


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
            import yfinance as yf_module

            if hasattr(yf_module, "shared") and hasattr(yf_module.shared, "_ERRORS"):
                if yf_symbol in yf_module.shared._ERRORS:
                    err_msg = str(yf_module.shared._ERRORS[yf_symbol]).lower()
                    if "no data found" in err_msg or "delisted" in err_msg:
                        raise ValueError("SYMBOL_NOT_FOUND")
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
        if "SYMBOL_NOT_FOUND" in str(e):
            raise
        logger.error(f"[{yf_symbol}] yfinance quote 失敗: {e}")
        return {}


async def get_quote(symbol: str) -> Dict[str, Any]:
    """取得即時報價 (非同步)。對於指數型標的，強制轉向 yfinance。"""
    symbol = _sanitize_ticker(symbol)
    now = time.time()
    if symbol in _quote_cache:
        val, expiry = _quote_cache[symbol]
        if now < expiry:
            return val  # type: ignore

    async def _fetch() -> Any:
        if symbol.startswith("^") or symbol == "VIX" or symbol.endswith("=F"):
            return await get_yfinance_quote(symbol)

        if is_finnhub_rate_limited():
            logger.warning(
                f"[{symbol}] Finnhub 處於限流冷卻中，直接轉向 yfinance fallback，避免堆積等待"
            )
            return await get_yfinance_quote(symbol)

        client = _get_client()
        try:
            data = await _execute_api_call(client.quote, symbol)
            if data and data.get("c", 0) > 0:
                return data

            # 若 Finnhub 回傳無效或報權限錯誤 (c=0 有可能是權限問題或標的不存在)
            # 快速驗證標的是否存在，避免在無效標的上耗費 yfinance 的長時間請求
            lookup = await _execute_api_call(client.symbol_lookup, symbol)
            if lookup and lookup.get("count", 0) == 0:
                logger.warning(f"[{symbol}] Finnhub 確認標的不存在，直接中斷")
                raise ValueError("SYMBOL_NOT_FOUND")

            # 嘗試作為 fallback 轉向 yfinance
            logger.warning(f"[{symbol}] Finnhub quote 無效，嘗試 yfinance fallback")
            return await get_yfinance_quote(symbol)
        except Exception as e:
            if "SYMBOL_NOT_FOUND" in str(e):
                raise
            # 任何 Finnhub 錯誤（包含限流 429、權限問題等）皆強制轉向 yfinance fallback，避免回傳 {} 導致下游算式崩潰
            logger.warning(
                f"[{symbol}] Finnhub quote 失敗: {e}，強制轉向 yfinance fallback"
            )
            return await get_yfinance_quote(symbol)

    try:
        res = await _fetch()
    except Exception as ex:
        _quote_cache[symbol] = ({}, now + _QUOTE_CACHE_TTL)
        if "SYMBOL_NOT_FOUND" in str(ex):
            raise
        return {}

    if res and res.get("c", 0) > 0:
        _quote_cache[symbol] = (res, now + _QUOTE_CACHE_TTL)
    else:
        _quote_cache[symbol] = ({}, now + _QUOTE_CACHE_TTL)
    return res  # type: ignore


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
        if "SYMBOL_NOT_FOUND" in str(e):
            logger.warning(f"[{symbol}] API 回傳標的不存在，直接中斷後續動作")
            return False
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
    symbol: str, period: str = "1y", interval: str = "1d", force_refresh: bool = False
) -> pd.DataFrame:
    """
    使用 yfinance 抓取歷史 K 線 (異步化，支援 4 小時快取與 Copy 隔離)。

    `force_refresh=True` 會略過快取讀取（但仍會將新結果寫入快取供其他呼叫端
    受益），供對資料新鮮度要求較高的短週期呼叫端使用（例如 15 分鐘價量警報）。
    """
    symbol = _to_yfinance_symbol(symbol)
    cache_key = (symbol, period, interval)
    now = time.time()

    if not force_refresh and cache_key in _history_cache:
        cached_df, expiry = _history_cache[cache_key]
        if now < expiry:
            return cached_df.copy()

    try:
        ticker = yf.Ticker(symbol)
        df = await _safe_yf_history(ticker, period=period, interval=interval)

        if df is None or getattr(df, "empty", True):
            logger.warning(
                f"[{symbol}] yfinance 歷史數據為空 (period={period}, interval={interval})"
            )
            empty_df = pd.DataFrame()
            _history_cache[cache_key] = (empty_df, now + _HISTORY_CACHE_TTL)
            return empty_df

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
        splits = await call_yf(lambda: ticker.splits)
        if splits is None:
            return pd.Series(dtype=float)
        return splits
    except Exception as e:
        logger.error(f"[{symbol}] yfinance 獲取拆股歷史失敗: {e}")
        return pd.Series(dtype=float)


OptionChainData = namedtuple("OptionChainData", ["calls", "puts", "underlying"])


async def _retry_once(
    coro_factory: Any, *, delay: float = 0.75, label: str = ""
) -> Any:
    """對指定的協程工廠函式重試一次（短暫固定延遲後重試）。僅用於期權到期日
    /期權鏈抓取三層降級架構「單一層內部」的暫時性故障重試，非通用重試機制，
    不影響降級層級的順序或本身的 try/except 結構。"""
    try:
        return await coro_factory()
    except Exception as e:
        logger.warning(f"{label} 第一次嘗試失敗 ({e})，{delay}s 後重試一次...")
        await asyncio.sleep(delay)
        return await coro_factory()


async def get_all_option_expiries(symbol: str) -> List[str]:
    """取得該標的所有可用的期權到期日 (支援 12 小時快取)。"""
    symbol = _sanitize_ticker(symbol)
    now = time.time()
    if symbol in _option_expiries_cache:
        cached_val, expiry = _option_expiries_cache[symbol]
        if now < expiry:
            return list(cached_val)

    res = []
    from config import TUNNEL_URL
    import urllib.parse

    base_url = (
        getattr(TUNNEL_URL, "rstrip", lambda x: TUNNEL_URL)("/") if TUNNEL_URL else ""
    )
    if base_url:
        req_url = (
            f"{base_url}/api/v1/scrape/yf/options/{urllib.parse.quote(symbol)}/expiries"
        )
        try:
            async with get_edge_client() as client:
                resp = await _retry_once(
                    lambda: client.get(req_url),
                    label=f"[{symbol}] Edge 節點抓取期權到期日",
                )
                if resp.status_code == 200:
                    data = resp.json()
                    if data.get("status") == "success" and data.get("data"):
                        res = data.get("data", [])
                        logger.info(f"[{symbol}] Edge 節點成功抓取期權到期日")
        except Exception as ex:
            logger.warning(f"[{symbol}] Edge 節點即時抓取期權到期日失敗: {ex}")

    if not res:
        if base_url:
            logger.info(f"[{symbol}] 降級改用本地 yfinance 直連抓取期權到期日...")
        try:
            ticker = yf.Ticker(symbol)
            expiries = await _retry_once(
                lambda: call_yf(lambda: ticker.options),
                label=f"[{symbol}] yfinance 抓取期權到期日",
            )
            res = list(expiries)
        except Exception as e:
            logger.warning(f"[{symbol}] yfinance 獲取期權到期日失敗: {e}")

    if res:
        _option_expiries_cache[symbol] = (
            list(res),
            now + _OPTION_EXPIRIES_CACHE_TTL,
        )
    return res


async def _fetch_option_chain_raw(
    symbol: str, expiry: str, force_live: bool = False
) -> Optional[Any]:
    """底層期權鏈抓取實作（優先 Edge Snapshot 快照 -> Edge 即時 Scrape -> 本地
    yfinance 直連）。force_live=True 時完全略過 Edge Snapshot 這一層（最舊可能
    達 _EDGE_SNAPSHOT_MAX_AGE_SECONDS），直接進行 Edge 即時 scrape 或本地
    yfinance 直連，保證回傳的是即時抓取的資料，僅供已透過 Discord defer
    (不受 3 秒互動逾時限制) 的深度分析路徑使用。"""
    calls_full = None
    puts_full = None
    underlying_full: Optional[Any] = None

    # 優先讀取 edge 背景排程寫入的 Option Chain 快照（毫秒級 SQLite 讀取），
    # 命中且夠新鮮就直接採用，跳過下方的 edge 即時 scrape / yfinance 直連。
    if not force_live:
        from services import edge_cache_client

        edge_cached_chain = await edge_cache_client.get_cached_option_chain(
            symbol, expiry
        )
        if edge_cached_chain is not None:
            edge_age = edge_cached_chain.get("age_seconds")
            if edge_age is not None and edge_age < _EDGE_SNAPSHOT_MAX_AGE_SECONDS:
                edge_data = edge_cached_chain["data"]
                edge_calls = pd.DataFrame(edge_data.get("calls", []))
                edge_puts = pd.DataFrame(edge_data.get("puts", []))
                if not (edge_calls.empty and edge_puts.empty):
                    calls_full = edge_calls
                    puts_full = edge_puts
                    underlying_full = {}

    if calls_full is None or puts_full is None:
        from config import TUNNEL_URL
        import urllib.parse

        base_url = (
            getattr(TUNNEL_URL, "rstrip", lambda x: TUNNEL_URL)("/")
            if TUNNEL_URL
            else ""
        )
        if base_url:
            req_url = f"{base_url}/api/v1/scrape/yf/options/{urllib.parse.quote(symbol)}/chain?expiry={expiry}"
            try:
                async with get_edge_client() as client:
                    resp = await _retry_once(
                        lambda: client.get(req_url),
                        label=f"[{symbol}] Edge 節點抓取期權鏈",
                    )
                    if resp.status_code == 200:
                        data = resp.json()
                        if data.get("status") == "success" and data.get("data"):
                            calls_full = pd.DataFrame(data["data"].get("calls", []))
                            puts_full = pd.DataFrame(data["data"].get("puts", []))
                            underlying_full = {}
                            logger.info(f"[{symbol}] Edge 節點成功抓取期權鏈")
            except Exception as ex:
                logger.warning(f"[{symbol}] Edge 節點即時抓取期權鏈失敗: {ex}")

        if (
            calls_full is None
            or puts_full is None
            or (calls_full.empty and puts_full.empty)
        ):
            if base_url:
                logger.info(
                    f"[{symbol}] 降級改用本地 yfinance 直連抓取期權鏈 (expiry={expiry})..."
                )
            try:
                ticker = yf.Ticker(symbol)
                chain = await _retry_once(
                    lambda: call_yf(ticker.option_chain, expiry),
                    label=f"[{symbol}] yfinance 抓取期權鏈 (expiry={expiry})",
                )
                if chain is not None:
                    calls_full = chain.calls.copy() if chain.calls is not None else None
                    puts_full = chain.puts.copy() if chain.puts is not None else None
                    underlying_full = (
                        chain.underlying.copy()
                        if hasattr(chain.underlying, "copy")
                        else chain.underlying
                    )
            except Exception as e:
                logger.warning(f"[{symbol}] 獲取期權鏈失敗 (expiry={expiry}): {e}")

    if (
        calls_full is not None
        and puts_full is not None
        and not (calls_full.empty and puts_full.empty)
    ):
        return OptionChainData(
            calls=calls_full, puts=puts_full, underlying=underlying_full
        )
    return None


async def get_option_chain(
    symbol: str,
    expiry: str,
    prune_pct: Optional[float] = 0.1,
    force_live: bool = False,
) -> Optional[Any]:
    """取得指定到期日的期權鏈 (支援 60 秒請求去重快取、SingleFlight 併發合併與
    Copy 隔離；資料新鮮度由下游 _fetch_option_chain_raw() 依 edge 快照
    age_seconds 把關，不再由本層快取 TTL 決定)。

    force_live=True 時完全略過本層的 60 秒請求去重快取讀取，並要求下游
    _fetch_option_chain_raw() 略過 Edge Snapshot 分層，保證回傳即時抓取的
    資料。僅供已透過 Discord defer（不受 3 秒互動逾時限制）的深度分析路徑
    使用；仍會把結果寫回 60 秒去重快取，讓同一視窗內的其他一般呼叫端受益於
    更新鮮的資料。"""
    symbol = _sanitize_ticker(symbol)
    cache_key = (symbol, expiry)
    now = time.time()

    async def _get_spot() -> float:
        try:
            quote = await get_quote(symbol)
            return float(quote.get("c", 0.0)) if quote else 0.0
        except Exception as e:
            logger.warning(
                f"[{symbol}] Strike Pruning 報價獲取失敗 ({e})，降級為不裁減全量資料。"
            )
            return 0.0

    def _prune(
        calls: Optional[pd.DataFrame],
        puts: Optional[pd.DataFrame],
        spot: float,
        pct: Optional[float],
    ) -> tuple[Optional[pd.DataFrame], Optional[pd.DataFrame]]:
        if spot <= 0.0 or pct is None:
            return calls, puts
        lower = spot * (1.0 - pct)
        upper = spot * (1.0 + pct)
        if calls is not None and not calls.empty:
            calls = calls[(calls["strike"] >= lower) & (calls["strike"] <= upper)]
        if puts is not None and not puts.empty:
            puts = puts[(puts["strike"] >= lower) & (puts["strike"] <= upper)]
        return calls, puts

    cached_val = None
    if not force_live and cache_key in _option_chain_cache:
        val, expiry_time = _option_chain_cache[cache_key]
        if now < expiry_time:
            cached_val = val

    if cached_val is None:
        from services.single_flight import SingleFlightManager

        # force_live 併入 SingleFlight key，避免與同一時間非強制即時的呼叫
        # 共用同一個進行中的請求而意外拿到 Edge Snapshot 分層的舊資料。
        single_flight_key = (
            f"opt_chain_raw_{symbol}_{expiry}_{'live' if force_live else 'std'}"
        )
        cached_val = await SingleFlightManager.run(
            single_flight_key,
            _fetch_option_chain_raw,
            symbol,
            expiry,
            force_live,
        )
        if cached_val is not None:
            _option_chain_cache[cache_key] = (
                cached_val,
                now + _OPTION_CHAIN_CACHE_TTL,
            )
            try:
                fingerprint = _compute_option_chain_fingerprint(
                    cached_val.calls, cached_val.puts
                )
                prev_fingerprint = _option_chain_fingerprint_cache.get(cache_key)
                if prev_fingerprint is not None and prev_fingerprint == fingerprint:
                    logger.warning(
                        f"[{symbol}] 期權鏈 OI/IV 指紋與上次抓取完全相同，"
                        f"Yahoo 端資料本輪可能尚未真正刷新（本次抓取仍是延遲快照）。"
                    )
                _option_chain_fingerprint_cache[cache_key] = fingerprint
            except Exception as fp_err:
                logger.debug(f"[{symbol}] 期權鏈指紋比對失敗（不影響主流程）: {fp_err}")

    if cached_val is not None:
        calls_copy = cached_val.calls.copy() if cached_val.calls is not None else None
        puts_copy = cached_val.puts.copy() if cached_val.puts is not None else None

        spot_price = await _get_spot()
        calls_copy, puts_copy = _prune(calls_copy, puts_copy, spot_price, prune_pct)

        underlying_copy = (
            cached_val.underlying.copy()
            if hasattr(cached_val.underlying, "copy")
            else cached_val.underlying
        )
        return OptionChainData(
            calls=calls_copy, puts=puts_copy, underlying=underlying_copy
        )

    return None


# 限制快取大小以節省記憶體 (1GB RAM VPS 優化)
MAX_CACHE_SIZE = 500

# BoundedCache 的實作已集中到 services/bounded_cache.py（過去這裡與
# polymarket_service.py 各自維護一份完全相同的 class，容量上限已分歧且無文件
# 說明理由，見上方 import）。保留模組層級名稱 BoundedCache，維持既有
# `from services.market_data_service import BoundedCache` 呼叫端不需變動。

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
# 60 秒：這層快取的用途改為「同一輪評估內的請求去重」，不再是「資料新鮮度保證」。
# 新鮮度改由 _fetch_option_chain_raw() 讀取 edge 背景輪詢寫入的 SQLite 快照時
# 附帶的 age_seconds 直接把關（該快照本身已由 nexus_edge_scraper/scheduler.py
# 每輪抓最新資料維持在 ~25-30 分鐘內，且該次讀取是毫秒級本地讀取，不是需要用長
# TTL 保護的昂貴資源）。舊值 900 秒（15 分鐘）會讓 get_option_chain() 在 edge
# 快照早就刷新之後，仍多疊加最多 15 分鐘才回頭去看新資料，等於在 edge 端的延遲
# 之外又白白多疊加一層。60 秒只是用來合併同一次心跳評估中，多個下游模組
# （max_pain.py、iv_metrics.py、uoa_detector.py 等）對同一 symbol+expiry 短時間
# 內重複呼叫 get_option_chain() 的情況，避免重複打 edge 快照讀取請求。
_OPTION_CHAIN_CACHE_TTL = 60  # 60 秒（僅請求去重，非新鮮度保證）

# 30 分鐘：_fetch_option_chain_raw() 用來判斷 edge 快照是否還「夠新鮮」可直接採用
# 的門檻，對齊 nexus_edge_scraper/scheduler.py 背景輪詢設計目標的整份清單輪替
# 週期（~25-30 分鐘，持倉標的因不受批次輪替影響則遠低於此）。舊值 3600 秒（1 小時）
# 比背景輪詢自己的正常週期寬鬆兩倍以上，等於這道 fallback 保護網在背景輪詢正常
# 運作時幾乎不會被觸發；調緊到 1800 秒後，只要某標的的批次輪替真的落後正常週期
# （例如背景輪詢卡住、Playwright 持續失敗），nexus_core 就會主動觸發下方的即時
# scrape 補上，而不是照單全收一份可能將近 1 小時舊的資料。
_EDGE_SNAPSHOT_MAX_AGE_SECONDS = 1800  # 30 分鐘

# --- 期權鏈「資料是否真的刷新過」觀測用指紋快取 ---
# yfinance/Yahoo 不提供 chain 層級的資料快照時間戳（各 contract 的 lastTradeDate 只反映
# 該合約最後成交時間，冷門合約可能是好幾天前，不能拿來判斷整條鏈是否已刷新）。因此無法在
# 抓取「之前」保證這次拿到的資料已經過了一次刷新週期，只能靠 _fetch_option_chain_raw()
# 讀取的 edge 快照 age_seconds 做時間上的把關。這裡額外做「抓取之後」的觀測：比對本次與
# 上次成功抓取的 OI/IV 指紋，若完全相同就記錄 warning，代表 Yahoo 端資料這一輪可能尚未
# 真正刷新（純觀測用，不重試、不阻擋主流程，只用來驗證背景輪詢節奏是否與實際資料延遲相符）。
_option_chain_fingerprint_cache = BoundedCache(max_size=MAX_CACHE_SIZE)


def _compute_option_chain_fingerprint(
    calls: Optional[pd.DataFrame], puts: Optional[pd.DataFrame]
) -> tuple[Any, ...]:
    def _rows(df: Optional[pd.DataFrame]) -> tuple[Any, ...]:
        if df is None or df.empty:
            return ()
        cols = [
            c
            for c in ("strike", "openInterest", "impliedVolatility")
            if c in df.columns
        ]
        if "strike" not in cols:
            return ()
        sub = df[cols].fillna(-1.0).sort_values("strike")
        return tuple(tuple(row) for row in sub.itertuples(index=False, name=None))

    return (_rows(calls), _rows(puts))


def clear_quote_cache() -> None:
    _quote_cache.clear()
    logger.info("Clarified quote cache")


def clear_profile_cache() -> None:
    _profile_cache.clear()
    logger.info("Clarified profile cache")


def clear_etf_cache() -> None:
    _etf_cache.clear()
    logger.info("Clarified ETF cache")


def clear_history_cache() -> None:
    _history_cache.clear()
    logger.info("Clarified history cache")


def clear_options_cache() -> None:
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
            return cached_val  # type: ignore

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


def clear_sma_cache() -> None:
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
            return val  # type: ignore

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


def clear_ema_cache() -> None:
    _ema_cache.clear()
    logger.info("Clarified EMA cache")


def run_garbage_collection() -> None:
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
