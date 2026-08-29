"""market_data_service 共用核心：互動請求標記、ticker 清洗、Finnhub/yfinance
rate limiting、Edge Scraper HTTP 連線池、以及 `_execute_api_call` 生產等級防禦
封裝。所有其他子模組 (quote/history/options/fundamentals) 皆透過此模組存取
Finnhub client 與節流機制，維持單一權威來源。
"""

from typing import Any, Optional
import asyncio
import contextvars
import functools
import logging
import random
import time
import weakref
from contextlib import contextmanager

import finnhub
from aiolimiter import AsyncLimiter

from config import FINNHUB_API_KEY

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
# （單元測試也會 patch 這個變數以驗證行為 —— 針對這個 `global` 變數的測試必須
# patch `services.market_data_service._core._rate_limit_until`，而非套件層級的
# `services.market_data_service._rate_limit_until`，因為 `_execute_api_call()`
# 透過 `global` 讀寫的是本模組自己的命名空間。）
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
      熔斷檢查刻意排在進入 limiter_per_second / limiter_global 之前，避免熔斷時仍消耗
      全局配額（AsyncLimiter 一經 acquire 即無法歸還，見下方 `async with sem:` 區塊）。

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
            if is_interactive:
                # 互動請求路徑：先確認冷卻狀態再進入母令牌桶／每秒流控，
                # 避免快速熔斷時仍白白消耗全局配額（AsyncLimiter 一經 acquire 即無法歸還）
                now = time.time()
                rate_limit_until = _rate_limit_until
                if now < rate_limit_until:
                    logger.warning(
                        "🚨 限流鎖內確認 Finnhub 正處於冷卻中，互動請求快速熔斷"
                    )
                    raise Exception("Finnhub rate limited, fast-circuit to fallback")

                async with controls["limiter_per_second"]:
                    async with controls["limiter_global"]:
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
            else:
                async with controls["limiter_per_second"]:
                    async with controls["limiter_global"]:
                        # 背景任務須額外遵循背景次級配額 (12/60s)
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
