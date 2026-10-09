"""market_data_service 共用核心：ticker 清洗、Finnhub/yfinance 限流閘門接線、
Edge Scraper HTTP 連線池、以及 `_execute_api_call` 生產等級防禦封裝。所有其他
子模組 (quote/history/options/fundamentals) 皆透過此模組存取 Finnhub client 與
節流機制，維持單一權威來源。限流本體（佇列、配額、冷卻）在 `services/rate_gate.py`；
互動請求標記（`_is_interactive_request`／`mark_interactive_request`／`interactive`）
與 `parse_retry_after` 亦定義於該 leaf 模組，此處 re-export 同一個物件。
"""

from typing import Any, AsyncIterator, Optional
import asyncio
import logging
import random
import re
import weakref
from dataclasses import dataclass
from contextlib import asynccontextmanager

import finnhub

from config import FINNHUB_API_KEY
from services import api_budget
from services.rate_gate import (  # noqa: F401 (re-exported)
    RateGateCooldownError,
    RateGateError,
    RateGateQueueFullError,
    RateGateTimeoutError,
    _is_interactive_request,
    clock,
    get_gate,
    interactive,
    mark_interactive_request,
    parse_retry_after,
)

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


_client: Optional[finnhub.Client] = None


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


@asynccontextmanager
async def yahoo_slot() -> AsyncIterator[None]:
    """取得一個 Yahoo 請求名額（`rate_gate` 的 yahoo 閘門，依互動／背景挑通道）。

    所有 Yahoo 流量（本地 yfinance、core→edge 即時 scrape）都應包在這裡，
    共用同一份預算；`call_yf` 與 edge 路徑使用完全相同的閘門。

    **冷卻集中強制**：冷卻中（含排隊期間才開始的冷卻）閘門拋 `RateGateCooldownError`，
    此處轉成 `YahooRateLimitedError`；排隊逾時或佇列滿載屬暫時性失敗，轉成
    `YahooEdgeBusyError`（不冷卻、不直連）。所有經由本函式的呼叫點（`call_yf`、
    edge 路徑）因此都不需各自檢查。"""
    gate = get_gate("yahoo")
    try:
        ticket = await gate.acquire()
    except RateGateCooldownError as e:
        raise YahooRateLimitedError("Yahoo 限流冷卻中") from e
    except (RateGateTimeoutError, RateGateQueueFullError) as e:
        raise YahooEdgeBusyError(f"Yahoo 請求排隊逾時或佇列已滿：{e}") from e
    try:
        yield
    finally:
        gate.release(ticket)


class YahooRateLimitedError(Exception):
    """Yahoo 處於 429 冷卻（或 edge 回報限流）。呼叫端應回空／沿用快取，
    **不得**改用資料中心 IP 直連（更容易被封）。"""


# 獨立的 429 token：前後不得緊鄰數字，避免誤判含 epoch 時間戳的訊息（如 "1791429600"）
_HTTP_429_PATTERN = re.compile(r"(?<!\d)429(?!\d)")


def _has_http_429(msg: str) -> bool:
    """訊息是否含獨立的 `429` token（前後非數字）。"""
    return _HTTP_429_PATTERN.search(msg) is not None


def is_yf_rate_limit_error(exc: BaseException) -> bool:
    """yfinance 限流判斷：類別名 YFRateLimitError（含父類別）、訊息含
    Too Many Requests（不分大小寫）、或訊息含獨立的 429 token。"""
    # 以類別名稱比對（含父類別），不直接 import yfinance.exceptions：
    # 舊版 yfinance 無此類別，且該子模組無型別 stub
    if any(c.__name__ == "YFRateLimitError" for c in type(exc).__mro__):
        return True
    msg = str(exc)
    return "too many requests" in msg.lower() or _has_http_429(msg)


def is_yahoo_rate_limited() -> bool:
    """檢查 Yahoo 是否正處於全域 429 冷卻中（edge 與本地直連共用）。"""
    return get_gate("yahoo").in_cooldown()


def mark_yahoo_rate_limited(retry_after: float | None = None) -> None:
    """標記 Yahoo 進入冷卻。有 Retry-After 以其為準；否則 60→120→…上限 900 秒指數退避。

    已在冷卻中（同一波多個在途請求各自 429）時**不升級退避倍數**，僅在
    Retry-After 指向更晚的時間時延長冷卻。實作委派給 yahoo 閘門的 `trip()`。"""
    get_gate("yahoo").trip(retry_after)


def mark_yahoo_ok(request_started_at: float) -> None:
    """Yahoo 成功回應即重置指數退避（不縮短既有冷卻到期時間）。

    `request_started_at` 為該請求「送出前」的 `rate_gate.clock()`；仍在冷卻中或請求
    不晚於最近一次 429 時不重置（見 `RateGate.mark_ok`）。"""
    get_gate("yahoo").mark_ok(request_started_at)


def note_yahoo_rate_limited(endpoint: str, retry_after: float | None = None) -> None:
    """記錄一次 Yahoo 429（計入配額摘要）並啟動／延長全域冷卻。"""
    api_budget.record_rate_limited("yahoo", endpoint)
    mark_yahoo_rate_limited(retry_after)


class YahooEdgeBusyError(Exception):
    """Edge 回報忙碌（HTTP 503／status=busy，排隊逾時）。屬暫時性失敗：呼叫端回
    None，**不得**降級資料中心直連，但也不啟動冷卻。"""


@dataclass(frozen=True)
class EdgeYahooResponse:
    """`edge_get_yahoo` 的回傳：原始 response 與已解析的 JSON（非 200 或解析失敗為 None）。
    呼叫端直接用 `data`，不要再對 `response` 呼叫 `.json()`。"""

    response: Any
    data: Any


async def edge_get_yahoo(client: Any, url: str, endpoint: str) -> EdgeYahooResponse:
    """core→edge 的 Yahoo 即時 scrape 請求共用流程（K 線與期權共用）。

    冷卻中直接拋 `YahooRateLimitedError`（進場前快速路徑；`yahoo_slot` 拿到名額後
    會再檢查一次）；送出時佔用 `yahoo_slot` 並以 `endpoint`（edge_history／
    edge_options）計入配額觀測；HTTP 429 或 JSON status=rate_limited 記錄並啟動
    全域冷卻後拋 `YahooRateLimitedError`；HTTP 503、status=busy 或 **請求逾時**
    （httpx.TimeoutException，edge 已設定但回應不及）拋 `YahooEdgeBusyError`；
    JSON status=success 重置退避。只有連線失敗（如 ConnectError，edge 不可達）
    才原樣外拋，讓呼叫端維持直連降級。回傳 `EdgeYahooResponse`（JSON 僅解析一次）。"""
    import httpx

    if is_yahoo_rate_limited():
        raise YahooRateLimitedError("Yahoo 限流冷卻中")
    async with yahoo_slot():
        started_at = clock()
        try:
            resp = await client.get(url)
        except httpx.TimeoutException as e:
            # edge 已設定但逾時：多半是 edge 端排隊／Yahoo 慢，改走資料中心直連只會更糟
            raise YahooEdgeBusyError("edge 請求逾時") from e
    # 只有拿到 edge 的 HTTP 回應才計入 Yahoo 呼叫；連線錯誤／逾時（請求未到 Yahoo）
    # 會在上面直接外拋而不計數，避免與之後的直連降級重複計算
    api_budget.record_call("yahoo", endpoint, interactive=_is_interactive_request.get())
    if resp.status_code == 429:
        note_yahoo_rate_limited(
            endpoint, parse_retry_after(getattr(resp, "headers", None))
        )
        raise YahooRateLimitedError("edge 回報 Yahoo 429")
    if resp.status_code == 503:
        raise YahooEdgeBusyError("edge 忙碌（503）")
    data: Any = None
    if resp.status_code == 200:
        try:
            data = resp.json()
        except Exception:
            data = None
        status = data.get("status") if isinstance(data, dict) else None
        if status == "rate_limited":
            note_yahoo_rate_limited(endpoint)
            raise YahooRateLimitedError("edge 回報 Yahoo rate_limited")
        if status == "busy":
            raise YahooEdgeBusyError("edge 忙碌（status=busy）")
        if status == "success":
            mark_yahoo_ok(started_at)
    return EdgeYahooResponse(response=resp, data=data)


async def call_yf(
    func: Any, *args: Any, _endpoint: str | None = None, **kwargs: Any
) -> Any:
    """統一節流包裝：所有對 yfinance 的 blocking 呼叫都應經過這裡。
    依 `_is_interactive_request` context 挑選互動或背景限流池。

    `_endpoint`（僅限關鍵字）只供 api_budget 計數命名，不會傳給 func；傳 lambda
    時務必指定，否則端點會全部歸到「<lambda>」而失去分析價值。
    """
    endpoint: str = _endpoint or str(getattr(func, "__name__", "yf"))
    async with yahoo_slot():
        started_at = clock()
        api_budget.record_call(
            "yahoo",
            endpoint,
            interactive=_is_interactive_request.get(),
        )
        try:
            result = await asyncio.to_thread(func, *args, **kwargs)
        except Exception as e:
            if is_yf_rate_limit_error(e):
                # 直連例外也走同一個限流判斷：計數並啟動全域冷卻（呼叫端不得重試）
                note_yahoo_rate_limited(endpoint)
            raise
        mark_yahoo_ok(started_at)
        return result


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
    return get_gate("finnhub").in_cooldown()


def is_finnhub_rate_limit_error(exc: BaseException) -> bool:
    """判斷例外是否為 Finnhub 限流類錯誤（HTTP 429、閘門拒絕或互動請求的快速熔斷例外）。

    - 閘門例外（`RateGateError`，冷卻／排隊逾時／佇列滿載）：僅 finnhub 來源算數。
    - 其餘依訊息：獨立的 `429` token（前後非數字，避免誤判 epoch 時間戳）、
      "limit reached"、"too many requests"，以及互動熔斷訊息
      （"Finnhub rate limited, fast-circuit to fallback"）。
    """
    if isinstance(exc, RateGateError):
        return exc.source == "finnhub"
    msg = str(exc).lower()
    return (
        _has_http_429(msg)
        or "limit reached" in msg
        or "too many requests" in msg
        or "fast-circuit" in msg
    )


def _is_finnhub_conn_error(exc: BaseException) -> bool:
    """連線類暫時性錯誤（逾時、斷線）；兩個通道皆重試。"""
    msg = str(exc).lower()
    return (
        "connection aborted" in msg
        or "timeout" in msg
        or "remotedisconnected" in msg
        or "temporarily unavailable" in msg
    )


# ---------------------------------------------------------------------------
# Core Async API Call (Thread-safe Wrapper)
# ---------------------------------------------------------------------------
async def _counted_finnhub_call(
    endpoint: str,
    is_interactive: bool,
    func: Any,
    args: tuple[Any, ...],
    kwargs: dict[str, Any],
) -> Any:
    """計數後在獨立線程執行 Finnhub 同步呼叫。"""
    api_budget.record_call("finnhub", endpoint, interactive=is_interactive)
    return await asyncio.to_thread(func, *args, **kwargs)


async def _execute_api_call(func: Any, *args, **kwargs) -> Any:  # type: ignore
    """執行 Finnhub API 呼叫的異步封裝（生產等級防禦）。

    限流、併發與冷卻全部由 `rate_gate` 的 finnhub 閘門負責（雙通道優先佇列、
    50 次/60s 窗口、互動保留 15、burst 3/s、併發 5/背景 2、排隊逾時與深度上限）。
    本函式只做：
    - 以 `slot` 包住實際請求；排隊期間不佔用在途名額，配額於核發當下才扣。
    - 429：`trip(retry_after)` 啟動全域冷卻。互動請求直接外拋，讓呼叫端無縫降級至
      yfinance；背景請求**離開 slot 後重新排隊**（冷卻中閘門會讓背景排隊等待），
      最多重試 3 次，每次重新受 max_wait 限制。
    - 連線錯誤／逾時（兩個通道皆然）：離開 slot 後睡 `2**attempt + jitter` 再重試，
      睡眠期間不佔用併發名額。
    - 互動請求遇冷卻中：閘門拋 `RateGateCooldownError`（訊息為
      "Finnhub rate limited, fast-circuit to fallback"），不消耗配額。
    """

    endpoint: str = str(getattr(func, "__name__", "unknown"))
    is_interactive = _is_interactive_request.get()
    gate = get_gate("finnhub")

    max_retries = 3
    for attempt in range(max_retries + 1):
        try:
            ticket = await gate.acquire()
        except RateGateCooldownError:
            logger.warning(
                "🚨 檢測到 Finnhub 正處於限流冷卻中，互動請求快速熔斷轉向 fallback"
            )
            raise

        conn_retry_delay: float | None = None
        try:
            started_at = clock()
            try:
                # Finnhub SDK 為同步阻塞 I/O，必須在獨立線程中執行
                result = await _counted_finnhub_call(
                    endpoint, is_interactive, func, args, kwargs
                )
            except Exception as e:
                is_rate_limit = is_finnhub_rate_limit_error(e)
                is_conn_error = _is_finnhub_conn_error(e)
                if is_rate_limit:
                    api_budget.record_rate_limited("finnhub", endpoint)
                    gate.trip(
                        parse_retry_after(
                            getattr(getattr(e, "response", None), "headers", None)
                        )
                    )
                if not (is_rate_limit or is_conn_error):
                    raise

                reason = "429 頻率限制" if is_rate_limit else "連線錯誤/超時"
                if attempt >= max_retries:
                    logger.error(
                        f"🚨 觸發 Finnhub {reason}。已達最大重試次數，放棄呼叫。"
                    )
                    raise
                if is_rate_limit and is_interactive:
                    # 互動路徑不 sleep 阻塞：立即快速熔斷，讓呼叫端降級至 yfinance
                    logger.warning(
                        "🚨 互動請求觸發 Finnhub 429 限流，立即快速熔斷並轉向 fallback"
                        f" (冷卻 {gate.cooldown_remaining():.1f}s)"
                    )
                    raise
                if is_rate_limit:
                    logger.warning(
                        f"🚨 觸發 Finnhub {reason}。離開名額重新排隊，冷卻約"
                        f" {gate.cooldown_remaining():.1f} 秒 (次數: {attempt + 1}/{max_retries})..."
                    )
                else:
                    conn_retry_delay = (2**attempt) + random.uniform(0.5, 1.5)
                    logger.warning(
                        f"🚨 觸發 Finnhub {reason}。將於 {conn_retry_delay:.1f} 秒後重試"
                        f" (次數: {attempt + 1}/{max_retries})..."
                    )
            else:
                gate.mark_ok(started_at)
                return result
        finally:
            gate.release(ticket)

        # 已離開 slot：連線錯誤在此睡眠（不佔併發名額）；429 直接重新排隊
        if conn_retry_delay is not None:
            await asyncio.sleep(conn_retry_delay)
