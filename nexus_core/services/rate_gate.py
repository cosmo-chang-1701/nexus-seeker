"""對外 API 統一限流閘門（RateGate）。

每個來源（Finnhub / Yahoo / SEC / LLM / FRED …）一個具名閘門，內含：

- 雙通道優先佇列（互動 interactive／背景 background，互動永遠優先）
- 滑動窗口配額、每秒 burst 上限、最小核發間隔
- 總併發與背景併發上限、互動保留額（背景不得動用窗口中最後 `interactive_reserve` 次）
- 全域 429 冷卻（Retry-After 優先，否則指數退避）
- 排隊逾時與佇列深度上限（超過即拋例外，不無限等待）
- 排隊等待、逾時、滿載、冷卻次數的觀測（`drain_stats()`，由 api_budget 每小時輸出）

設計重點：

- **配額只在核發當下扣**：核發時間戳、在途數 +1 與 `Future.set_result` 在同一個同步
  區段完成。冷卻熔斷、排隊逾時、取消都不會浪費配額（舊 AsyncLimiter「acquire 即
  無法歸還」的缺陷）。
- **狀態範圍**：佇列／窗口／在途數／計時器為「每個 event loop 一份」（WeakKeyDictionary）；
  冷卻與統計為 process 全域（`threading.Lock` 保護），DB writer 執行緒等其他 loop
  觸發的 `trip()` 會經 `call_soon_threadsafe` 喚醒主 loop 的佇列。額外的 loop 會有
  自己的窗口（與舊 AsyncLimiter 行為相同的已知限制）。
- 本模組為 leaf：不 import 任何 service（避免循環相依）；僅讀取 `config` 的 SEC 限速常數。
"""

import asyncio
import collections
import contextvars
import functools
import logging
import math
import threading
import time
import weakref
from contextlib import asynccontextmanager, contextmanager
from dataclasses import dataclass
from typing import Any, AsyncIterator, Iterator, Literal

import config

logger = logging.getLogger(__name__)

Lane = Literal["interactive", "background"]
_LANES: tuple[Lane, ...] = ("interactive", "background")


# ---------------------------------------------------------------------------
# 互動請求優先權標記（Context-local）
# ---------------------------------------------------------------------------
# 用於區分「使用者互動指令」（如 /x）與「背景排程」（心跳／掃描）對外部 API 的
# 呼叫來源，讓閘門替互動請求保留獨立額度與優先權，避免背景任務把共用額度佔滿、
# 導致互動指令長時間排隊卡住。透過 contextvars 傳遞：asyncio.gather／create_task
# 產生的子協程會自動繼承呼叫當下的 context，不需要逐層手動傳遞旗標。
_is_interactive_request: contextvars.ContextVar[bool] = contextvars.ContextVar(
    "is_interactive_request", default=False
)


@contextmanager
def mark_interactive_request() -> Iterator[None]:
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


def parse_retry_after(headers: Any) -> float | None:
    """從回應標頭取 Retry-After 秒數；缺少或非數字（如 HTTP date）回 None。"""
    try:
        raw = headers.get("Retry-After") or headers.get("retry-after")
        return float(raw) if raw else None
    except (AttributeError, TypeError, ValueError):
        return None


# 單調時鐘；測試可 patch `rate_gate._clock` 以操控冷卻與窗口時間
_clock = time.monotonic


def clock() -> float:
    """閘門使用的單調時鐘。`mark_ok(request_started_at)` 的時間戳須取自本函式。"""
    return _clock()


# ---------------------------------------------------------------------------
# 例外
# ---------------------------------------------------------------------------
class RateGateError(Exception):
    """閘門拒絕放行的共同基底；`source` 為閘門名稱。"""

    def __init__(self, source: str, message: str):
        super().__init__(message)
        self.source = source


class RateGateCooldownError(RateGateError):
    """來源處於 429 冷卻中，快速熔斷（不消耗配額）。"""


class RateGateTimeoutError(RateGateError):
    """排隊等待超過 max_wait（只計入列到核發，不含請求執行時間）。"""


class RateGateQueueFullError(RateGateError):
    """該通道佇列深度已滿，拒絕入列。"""


# ---------------------------------------------------------------------------
# 政策
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class GatePolicy:
    name: str
    window_limit: int  # 滑動窗口內最多核發次數
    window_seconds: float
    max_concurrency: int  # 總在途上限
    burst_limit: int | None = None  # 任意 1 秒內上限
    min_interval: float = 0.0  # 兩次核發最小間隔（取代 random pacing）
    background_concurrency: int | None = None  # 背景在途上限；None＝同 max_concurrency
    interactive_reserve: int = 0  # 窗口中背景不得動用的保留額
    max_wait_interactive: float = 15.0  # 只計「入列→核發」，不含請求執行時間
    max_wait_background: float = 180.0
    max_queue_depth_interactive: int = 50
    max_queue_depth_background: int = 200
    cooldown_initial: float = 60.0  # 無 Retry-After 時的指數退避起點
    cooldown_max: float = 600.0  # 退避與 Retry-After 的上限
    background_fail_fast_in_cooldown: bool = (
        True  # Yahoo/SEC/低頻=True；Finnhub/LLM=False
    )

    def __post_init__(self) -> None:
        if self.window_limit < 1 or self.max_concurrency < 1:
            raise ValueError(f"{self.name}: window_limit／max_concurrency 須 ≥ 1")
        if not 0 <= self.interactive_reserve < self.window_limit:
            raise ValueError(f"{self.name}: interactive_reserve 須在 [0, window_limit)")
        if self.bg_concurrency > self.max_concurrency:
            raise ValueError(
                f"{self.name}: background_concurrency 不得大於 max_concurrency"
            )

    @property
    def bg_concurrency(self) -> int:
        bg = self.background_concurrency
        return self.max_concurrency if bg is None else bg

    def max_wait(self, lane: Lane) -> float:
        return (
            self.max_wait_interactive
            if lane == "interactive"
            else self.max_wait_background
        )

    def max_depth(self, lane: Lane) -> int:
        return (
            self.max_queue_depth_interactive
            if lane == "interactive"
            else self.max_queue_depth_background
        )


# 各來源閘門參數（SSOT；說明見 docs/architecture/07_outbound_rate_gate.md）
POLICIES: dict[str, GatePolicy] = {
    p.name: p
    for p in (
        # Finnhub 免費方案 60 次/分，保留 10 次冗餘；互動保留 15，背景最多 35。
        # 互動等待 15s：/x 批次最多 15 標的併發、每秒 burst 3 → 15s 約可核發 45 次。
        GatePolicy(
            name="finnhub",
            window_limit=50,
            window_seconds=60.0,
            burst_limit=3,
            min_interval=0.05,
            max_concurrency=5,
            background_concurrency=2,
            interactive_reserve=15,
            max_wait_interactive=15.0,
            max_wait_background=180.0,
            cooldown_initial=5.0,
            cooldown_max=60.0,
            background_fail_fast_in_cooldown=False,
        ),
        # Yahoo 總量等同舊「互動 30 + 背景 30」雙桶；背景仍 ≤ 30/分。
        GatePolicy(
            name="yahoo",
            window_limit=60,
            window_seconds=60.0,
            max_concurrency=7,
            background_concurrency=2,
            interactive_reserve=30,
            max_wait_interactive=20.0,
            max_wait_background=180.0,
            cooldown_initial=60.0,
            cooldown_max=900.0,
        ),
        GatePolicy(
            name="sec",
            window_limit=config.SEC_LIMITER_MAX_RATE,
            window_seconds=1.0,
            max_concurrency=4,
            max_wait_interactive=30.0,
            max_wait_background=180.0,
            cooldown_initial=600.0,
            cooldown_max=1800.0,
        ),
        GatePolicy(
            name="llm",
            window_limit=30,
            window_seconds=60.0,
            max_concurrency=2,
            max_wait_interactive=60.0,
            max_wait_background=600.0,
            cooldown_initial=20.0,
            cooldown_max=300.0,
            background_fail_fast_in_cooldown=False,
        ),
        GatePolicy(
            name="fred",
            window_limit=30,
            window_seconds=60.0,
            max_concurrency=2,
            cooldown_initial=60.0,
            cooldown_max=600.0,
        ),
        GatePolicy(
            name="tsa",
            window_limit=20,
            window_seconds=60.0,
            max_concurrency=2,
            cooldown_initial=60.0,
            cooldown_max=600.0,
        ),
        GatePolicy(
            name="twse",
            window_limit=20,
            window_seconds=60.0,
            max_concurrency=2,
            cooldown_initial=60.0,
            cooldown_max=600.0,
        ),
        GatePolicy(
            name="tpex",
            window_limit=20,
            window_seconds=60.0,
            max_concurrency=2,
            cooldown_initial=60.0,
            cooldown_max=600.0,
        ),
        GatePolicy(
            name="polymarket",
            window_limit=120,
            window_seconds=60.0,
            min_interval=0.1,
            max_concurrency=3,
            max_wait_interactive=10.0,
            cooldown_initial=30.0,
            cooldown_max=300.0,
        ),
        GatePolicy(
            name="alpaca_rest",
            window_limit=150,
            window_seconds=60.0,
            max_concurrency=3,
            cooldown_initial=30.0,
            cooldown_max=300.0,
        ),
    )
}


# ---------------------------------------------------------------------------
# 觀測
# ---------------------------------------------------------------------------
_WAIT_SAMPLES_MAX = 2048


@dataclass(frozen=True)
class GateStats:
    """單一閘門自上次 `drain_stats()` 以來的累計。"""

    granted: int
    wait_p95: float
    wait_max: float
    timeouts: int
    queue_full: int
    cooldowns: int
    depth_peak: int

    def is_empty(self) -> bool:
        return not (
            self.granted
            or self.timeouts
            or self.queue_full
            or self.cooldowns
            or self.depth_peak
        )


class _StatsAcc:
    """統計累加器；僅在持有 `RateGate._lock` 時存取。"""

    def __init__(self) -> None:
        self.reset()

    def reset(self) -> None:
        self.granted = 0
        self.waits: collections.deque[float] = collections.deque(
            maxlen=_WAIT_SAMPLES_MAX
        )
        self.wait_max = 0.0
        self.timeouts = 0
        self.queue_full = 0
        self.cooldowns = 0
        self.depth_peak = 0

    def snapshot(self) -> GateStats:
        samples = sorted(self.waits)
        p95 = samples[max(math.ceil(0.95 * len(samples)) - 1, 0)] if samples else 0.0
        return GateStats(
            granted=self.granted,
            wait_p95=p95,
            wait_max=self.wait_max,
            timeouts=self.timeouts,
            queue_full=self.queue_full,
            cooldowns=self.cooldowns,
            depth_peak=self.depth_peak,
        )


# ---------------------------------------------------------------------------
# 每個 event loop 的佇列狀態
# ---------------------------------------------------------------------------
class Ticket:
    """一次排隊／核發的憑證；`slot()` 與 `acquire()/release()` 共用。"""

    __slots__ = ("fut", "lane", "enqueued_at", "state", "granted", "released")

    def __init__(
        self,
        fut: "asyncio.Future[None]",
        lane: Lane,
        enqueued_at: float,
        state: "_LoopState",
    ) -> None:
        self.fut = fut
        self.lane = lane
        self.enqueued_at = enqueued_at
        self.state = state
        self.granted = False  # pump 已核發（配額已扣、在途數已 +1）
        self.released = False


class _LoopState:
    """單一 event loop 內的佇列、窗口、在途數與計時器。只在該 loop 的執行緒存取
    （`pump` 由 `call_soon_threadsafe` 排入）。"""

    def __init__(self, gate: "RateGate", loop: asyncio.AbstractEventLoop) -> None:
        self.gate = gate
        # 弱參照：避免 WeakKeyDictionary 的 value 反向強參照 key 而使 loop 無法回收
        self.loop_ref: weakref.ReferenceType[asyncio.AbstractEventLoop] = weakref.ref(
            loop
        )
        self.queues: dict[Lane, collections.deque[Ticket]] = {
            "interactive": collections.deque(),
            "background": collections.deque(),
        }
        self.window: collections.deque[float] = collections.deque()
        self.last_grant: float | None = None
        self.in_flight = 0
        self.bg_in_flight = 0
        self._timer: asyncio.TimerHandle | None = None

    # -- 計時器（單一 handle） ------------------------------------------------
    def _cancel_timer(self) -> None:
        if self._timer is not None:
            self._timer.cancel()
            self._timer = None

    def _arm(self, delay: float) -> None:
        loop = self.loop_ref()
        if loop is None or loop.is_closed():
            return
        # 浮點／時鐘解析度保險：至少 0.5ms，避免零延遲空轉
        self._timer = loop.call_later(max(delay, 0.0005), self._on_timer)

    def _on_timer(self) -> None:
        self._timer = None
        self.pump()

    def cancel_timer(self) -> None:
        self._cancel_timer()

    # -- 佇列輔助 --------------------------------------------------------------
    def depth(self) -> int:
        return len(self.queues["interactive"]) + len(self.queues["background"])

    def _head(self, lane: Lane) -> Ticket | None:
        q = self.queues[lane]
        while q and q[0].fut.done():  # 已取消／已失敗者略過
            q.popleft()
        return q[0] if q else None

    def _has_waiters(self) -> bool:
        return self._head("interactive") is not None or (
            self._head("background") is not None
        )

    def _discard(self, ticket: Ticket) -> None:
        try:
            self.queues[ticket.lane].remove(ticket)
        except ValueError:
            pass

    # -- 核發規則 --------------------------------------------------------------
    def _earliest_ready(self, lane: Lane, now: float) -> float:
        """依 min_interval、burst、窗口配額（背景扣除互動保留額）算出最早可核發時間。"""
        p = self.gate.policy
        ready = now
        if p.min_interval > 0 and self.last_grant is not None:
            ready = max(ready, self.last_grant + p.min_interval)
        w = self.window
        if p.burst_limit is not None:
            recent = [t for t in w if t > now - 1.0]
            if len(recent) >= p.burst_limit:
                ready = max(ready, recent[len(recent) - p.burst_limit] + 1.0)
        cap = p.window_limit - (p.interactive_reserve if lane == "background" else 0)
        in_window = [t for t in w if t > now - p.window_seconds]
        if len(in_window) >= cap:
            ready = max(ready, in_window[len(in_window) - cap] + p.window_seconds)
        return ready

    def _trim_window(self, now: float) -> None:
        p = self.gate.policy
        horizon = max(p.window_seconds, 1.0 if p.burst_limit is not None else 0.0)
        w = self.window
        while w and w[0] <= now - horizon:
            w.popleft()

    def _fail_queued_in_cooldown(self) -> None:
        p = self.gate.policy
        for lane in _LANES:
            if lane == "background" and not p.background_fail_fast_in_cooldown:
                continue
            q = self.queues[lane]
            while q:
                t = q.popleft()
                if not t.fut.done():
                    t.fut.set_exception(self.gate._cooldown_error())

    def _grant(self, ticket: Ticket, now: float) -> None:
        """核發：時間戳、在途數與 set_result 在同一個同步區段完成。"""
        self.queues[ticket.lane].popleft()
        self.window.append(now)
        self.last_grant = now
        self.in_flight += 1
        if ticket.lane == "background":
            self.bg_in_flight += 1
        ticket.granted = True
        self.gate._note_grant(now - ticket.enqueued_at)
        ticket.fut.set_result(None)

    def pump(self) -> None:
        """喚醒佇列：入列、release、計時器到期與 trip 通知時執行。"""
        self._cancel_timer()
        loop = self.loop_ref()
        if loop is None or loop.is_closed():
            return
        gate = self.gate
        p = gate.policy
        now = gate.clock()

        cooldown_until = gate._cooldown_until_value()
        if now < cooldown_until:
            self._fail_queued_in_cooldown()
            if self._has_waiters():  # 例如 Finnhub／LLM 背景：排隊等冷卻結束
                self._arm(cooldown_until - now)
            return

        self._trim_window(now)
        while True:
            lane: Lane
            ticket = self._head("interactive")
            if ticket is not None:
                lane = "interactive"
            else:
                ticket = self._head("background")
                if ticket is None:
                    return
                lane = "background"
            if self.in_flight >= p.max_concurrency:
                return  # 等 release 喚醒
            if lane == "background" and self.bg_in_flight >= p.bg_concurrency:
                return
            ready = self._earliest_ready(lane, now)
            if ready - now > 1e-9:
                self._arm(ready - now)
                return
            self._grant(ticket, now)

    # -- 取得／釋放 ------------------------------------------------------------
    async def acquire(self, lane: Lane) -> Ticket:
        gate = self.gate
        p = gate.policy
        if gate.in_cooldown() and (
            lane == "interactive" or p.background_fail_fast_in_cooldown
        ):
            raise gate._cooldown_error()
        q = self.queues[lane]
        if len(q) >= p.max_depth(lane):
            gate._note_queue_full()
            raise RateGateQueueFullError(
                p.name, f"{p.name} 佇列已滿（{lane}，上限 {p.max_depth(lane)}）"
            )
        loop = asyncio.get_running_loop()
        ticket = Ticket(loop.create_future(), lane, gate.clock(), self)
        q.append(ticket)
        gate._note_depth(self.depth())
        self.pump()
        try:
            await asyncio.wait_for(ticket.fut, p.max_wait(lane))
        except BaseException as exc:
            # 逾時／取消與核發可能發生在同一輪：已核發就必須歸還在途名額，
            # 否則 slot 永久洩漏；未核發則把 waiter 從佇列移除。
            if ticket.granted:
                self.release(ticket)
            else:
                self._discard(ticket)
            if ticket.fut.done() and not ticket.fut.cancelled():
                ticket.fut.exception()  # 標記已取用，避免「never retrieved」警告
            if isinstance(exc, asyncio.TimeoutError):
                gate._note_timeout()
                raise RateGateTimeoutError(
                    p.name,
                    f"{p.name} 排隊逾時（{lane}，等待 {p.max_wait(lane):.0f} 秒仍未核發）",
                ) from None
            raise
        return ticket

    def release(self, ticket: Ticket) -> None:
        if ticket.released or not ticket.granted:
            return
        ticket.released = True
        self.in_flight -= 1
        if ticket.lane == "background":
            self.bg_in_flight -= 1
        self.pump()


# ---------------------------------------------------------------------------
# 閘門
# ---------------------------------------------------------------------------
class RateGate:
    def __init__(self, policy: GatePolicy) -> None:
        self.policy = policy
        self._lock = threading.Lock()
        self._states: weakref.WeakKeyDictionary[
            asyncio.AbstractEventLoop, _LoopState
        ] = weakref.WeakKeyDictionary()
        # process 全域（鎖內存取）
        self._cd_until = 0.0
        self._backoff = 0.0
        self._last_trip_at = 0.0
        self._stats = _StatsAcc()

    @staticmethod
    def clock() -> float:
        return clock()

    # -- 狀態 ------------------------------------------------------------------
    def _state(self) -> _LoopState:
        loop = asyncio.get_running_loop()
        with self._lock:
            st = self._states.get(loop)
            if st is None:
                st = _LoopState(self, loop)
                self._states[loop] = st
        return st

    def _cooldown_until_value(self) -> float:
        with self._lock:
            return self._cd_until

    def _cooldown_error(self) -> RateGateCooldownError:
        name = self.policy.name
        return RateGateCooldownError(
            name, f"{name.capitalize()} rate limited, fast-circuit to fallback"
        )

    # -- 取得／釋放名額 ----------------------------------------------------------
    @staticmethod
    def _current_lane() -> Lane:
        return "interactive" if _is_interactive_request.get() else "background"

    async def acquire(self) -> Ticket:
        """排隊取得一個名額；失敗拋 `RateGateError` 子類。成功後須呼叫 `release()`。"""
        return await self._state().acquire(self._current_lane())

    def release(self, ticket: Ticket) -> None:
        ticket.state.release(ticket)

    @asynccontextmanager
    async def slot(self) -> AsyncIterator[None]:
        """`async with gate.slot():` 包住一次實際對外請求；通道由
        `_is_interactive_request` 決定。排隊期間不佔用在途名額。"""
        ticket = await self.acquire()
        try:
            yield
        finally:
            self.release(ticket)

    # -- 冷卻 ------------------------------------------------------------------
    def in_cooldown(self) -> bool:
        return clock() < self._cooldown_until_value()

    def cooldown_remaining(self) -> float:
        return max(self._cooldown_until_value() - clock(), 0.0)

    def trip(self, retry_after: float | None = None) -> float:
        """標記來源限流並啟動／延長全域冷卻，回傳剩餘冷卻秒數。

        有 Retry-After 以其為準（上限 `cooldown_max`）；否則自 `cooldown_initial`
        指數退避。已在冷卻中（同一波多個在途請求各自 429）時**不升級退避倍數**，
        僅在 Retry-After 指向更晚的時間時延長。可從任何執行緒呼叫：會經
        `call_soon_threadsafe` 通知所有已註冊 loop 重新評估佇列。"""
        p = self.policy
        now = clock()
        with self._lock:
            self._last_trip_at = now
            extended = False
            started = False
            if now < self._cd_until:
                if retry_after is not None and retry_after > 0:
                    new_until = now + min(retry_after, p.cooldown_max)
                    if new_until > self._cd_until:
                        self._cd_until = new_until
                        extended = True
                delay = self._cd_until - now
            else:
                if retry_after is not None and retry_after > 0:
                    delay = min(retry_after, p.cooldown_max)
                else:
                    delay = (
                        p.cooldown_initial
                        if self._backoff <= 0
                        else min(self._backoff * 2, p.cooldown_max)
                    )
                    self._backoff = delay
                self._cd_until = max(self._cd_until, now + delay)
                self._stats.cooldowns += 1
                started = True
            remaining = self._cd_until - now
        if started:
            logger.warning(f"🚨 {p.name} 觸發限流，全域冷卻 {delay:.0f} 秒")
        elif extended:
            logger.warning(
                f"🚨 {p.name} 限流冷卻中收到 Retry-After，延長至 {remaining:.0f} 秒後"
            )
        self._notify_loops()
        return remaining

    def mark_ok(self, request_started_at: float) -> None:
        """成功回應即重置指數退避（不縮短既有冷卻到期時間）。

        `request_started_at` 為該請求「送出前」的 `rate_gate.clock()`。以下情況**不重置**，
        避免同一波 429 之前已送出、較晚才回來的成功請求把退避歸零：
        - 目前仍在冷卻中；
        - 請求送出時間不晚於最近一次 trip 時間。"""
        with self._lock:
            if clock() < self._cd_until:
                return
            if request_started_at <= self._last_trip_at:
                return
            self._backoff = 0.0

    def _notify_loops(self) -> None:
        with self._lock:
            states = list(self._states.values())
        for st in states:
            loop = st.loop_ref()
            if loop is None or loop.is_closed():
                continue
            try:
                loop.call_soon_threadsafe(st.pump)
            except RuntimeError:  # loop 在檢查後剛好關閉
                continue

    # -- 觀測 ------------------------------------------------------------------
    def _note_grant(self, wait: float) -> None:
        with self._lock:
            s = self._stats
            s.granted += 1
            s.waits.append(wait)
            if wait > s.wait_max:
                s.wait_max = wait

    def _note_timeout(self) -> None:
        with self._lock:
            self._stats.timeouts += 1

    def _note_queue_full(self) -> None:
        with self._lock:
            self._stats.queue_full += 1

    def _note_depth(self, depth: int) -> None:
        with self._lock:
            if depth > self._stats.depth_peak:
                self._stats.depth_peak = depth

    def drain_stats(self) -> GateStats:
        """回傳並歸零累計統計。"""
        with self._lock:
            snap = self._stats.snapshot()
            self._stats.reset()
        return snap

    def queue_depth(self, lane: Lane | None = None) -> int:
        """目前 event loop 的佇列深度（供測試與診斷）。"""
        st = self._state()
        return st.depth() if lane is None else len(st.queues[lane])

    def in_flight(self) -> int:
        """目前 event loop 的在途請求數（供測試與診斷）。"""
        return self._state().in_flight


# ---------------------------------------------------------------------------
# 模組層 registry
# ---------------------------------------------------------------------------
_GATES: dict[str, RateGate] = {}
_registry_lock = threading.Lock()


def get_gate(name: str) -> RateGate:
    """取得（必要時建立）具名閘門；名稱須在 `POLICIES` 中。"""
    with _registry_lock:
        gate = _GATES.get(name)
        if gate is None:
            gate = RateGate(POLICIES[name])
            _GATES[name] = gate
        return gate


def drain_stats() -> dict[str, GateStats]:
    """回傳所有有活動閘門的統計並歸零（供 api_budget 每小時摘要）。"""
    with _registry_lock:
        gates = list(_GATES.items())
    out: dict[str, GateStats] = {}
    for name, gate in gates:
        snap = gate.drain_stats()
        if not snap.is_empty():
            out[name] = snap
    return out


def reset_for_tests() -> None:
    """清空所有閘門、冷卻與統計（供測試）。"""
    with _registry_lock:
        gates = list(_GATES.values())
        _GATES.clear()
    for gate in gates:
        with gate._lock:
            states = list(gate._states.values())
        for st in states:
            loop = st.loop_ref()
            if loop is not None and not loop.is_closed():
                try:
                    loop.call_soon_threadsafe(st.cancel_timer)
                except RuntimeError:
                    pass
