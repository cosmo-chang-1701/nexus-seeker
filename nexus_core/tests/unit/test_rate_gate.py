"""services/rate_gate：雙通道優先佇列、窗口配額、保留額、併發、冷卻、逾時與競態。

時間相關案例以 monkeypatch `rate_gate._clock` 的假時鐘搭配手動 `state.pump()` 驅動，
不依賴真實 sleep；僅少數案例（min_interval、保留額）使用極短的真實窗口。
"""

import asyncio
import threading
from collections.abc import Callable, Iterator
from typing import Any

import pytest

from services import rate_gate
from services.rate_gate import (
    GatePolicy,
    RateGate,
    RateGateCooldownError,
    RateGateQueueFullError,
    RateGateTimeoutError,
    Ticket,
    mark_interactive_request,
)


@pytest.fixture(autouse=True)
def _clean_gates() -> Iterator[None]:
    rate_gate.reset_for_tests()
    yield
    rate_gate.reset_for_tests()


@pytest.fixture
def fake_clock(monkeypatch: pytest.MonkeyPatch) -> list[float]:
    t = [1000.0]
    monkeypatch.setattr(rate_gate, "_clock", lambda: t[0])
    return t


def _policy(**kw: Any) -> GatePolicy:
    base: dict[str, Any] = dict(
        name="t",
        window_limit=100,
        window_seconds=60.0,
        max_concurrency=10,
        max_wait_interactive=5.0,
        max_wait_background=5.0,
    )
    base.update(kw)
    return GatePolicy(**base)


async def _acq(gate: RateGate, interactive: bool = False) -> Ticket:
    if interactive:
        with mark_interactive_request():
            return await gate.acquire()
    return await gate.acquire()


async def _settle(n: int = 5) -> None:
    for _ in range(n):
        await asyncio.sleep(0)


def _spawn(gate: RateGate, interactive: bool = False) -> "asyncio.Task[Ticket]":
    return asyncio.create_task(_acq(gate, interactive))


# ---------------------------------------------------------------------------
# 優先權與核發規則
# ---------------------------------------------------------------------------
async def test_interactive_jumps_ahead_of_queued_background() -> None:
    gate = RateGate(_policy(max_concurrency=1))
    holder = await gate.acquire()
    order: list[str] = []

    async def worker(name: str, interactive: bool) -> None:
        t = await _acq(gate, interactive)
        order.append(name)
        gate.release(t)

    bg = [asyncio.create_task(worker(f"bg{i}", False)) for i in range(2)]
    await _settle()
    inter = asyncio.create_task(worker("inter", True))
    await _settle()
    assert gate.queue_depth("background") == 2
    assert gate.queue_depth("interactive") == 1

    gate.release(holder)
    await asyncio.wait_for(asyncio.gather(*bg, inter), 2)
    assert order == ["inter", "bg0", "bg1"]


async def test_interactive_reserve_blocks_background_but_not_interactive(
    fake_clock: list[float],
) -> None:
    # 窗口 4、保留 2：背景最多 2 次，互動可用滿 4 次
    gate = RateGate(_policy(window_limit=4, interactive_reserve=2))
    state = gate._state()
    t1 = await _acq(gate)
    t2 = await _acq(gate)
    bg3 = _spawn(gate)
    await _settle()
    assert not bg3.done()  # 被保留額擋下
    assert gate.queue_depth("background") == 1

    i1 = await asyncio.wait_for(_acq(gate, True), 1)
    i2 = await asyncio.wait_for(_acq(gate, True), 1)
    i3 = _spawn(gate, True)
    await _settle()
    assert not i3.done()  # 互動也受窗口總量限制
    assert len(state.window) == 4

    fake_clock[0] += 61  # 窗口滑過
    state.pump()
    await asyncio.wait_for(asyncio.gather(bg3, i3), 1)
    for t in (t1, t2, i1, i2, bg3.result(), i3.result()):
        gate.release(t)
    assert gate.in_flight() == 0


async def test_burst_limit_enforced_per_second(fake_clock: list[float]) -> None:
    gate = RateGate(_policy(burst_limit=2))
    state = gate._state()
    a = await _acq(gate)
    b = await _acq(gate)
    c = _spawn(gate)
    await _settle()
    assert not c.done()

    fake_clock[0] += 0.5
    state.pump()
    await _settle()
    assert not c.done()  # 1 秒內仍超過 burst

    fake_clock[0] += 0.6
    state.pump()
    ticket_c = await asyncio.wait_for(c, 1)
    for t in (a, b, ticket_c):
        gate.release(t)


async def test_min_interval_spaces_grants() -> None:
    gate = RateGate(_policy(min_interval=0.03))
    stamps: list[float] = []
    for _ in range(4):
        t = await gate.acquire()
        stamps.append(rate_gate.clock())
        gate.release(t)
    gaps = [b - a for a, b in zip(stamps, stamps[1:])]
    assert all(g >= 0.03 - 0.003 for g in gaps), gaps


async def test_total_and_background_concurrency_caps() -> None:
    gate = RateGate(_policy(max_concurrency=3, background_concurrency=2))
    bg = [_spawn(gate) for _ in range(4)]
    await _settle()
    assert gate.in_flight() == 2  # 背景上限 2
    assert gate.queue_depth("background") == 2

    inter = [_spawn(gate, True) for _ in range(3)]
    await _settle()
    assert gate.in_flight() == 3  # 總上限 3：僅 1 個互動補上
    assert gate.queue_depth("interactive") == 2

    tasks = bg + inter
    assert sum(1 for t in tasks if t.done()) == 3
    # 逐一釋放，在途數永不超過 3，且最終全部被服務
    released: set[asyncio.Task[Ticket]] = set()
    for _ in range(50):
        assert gate.in_flight() <= 3
        ready = [t for t in tasks if t.done() and t not in released]
        if ready:
            released.add(ready[0])
            gate.release(ready[0].result())
        elif len(released) == len(tasks):
            break
        await _settle()
    assert len(released) == len(tasks)
    assert gate.in_flight() == 0


async def test_pump_skips_cancelled_waiters_without_leaking() -> None:
    gate = RateGate(_policy(max_concurrency=1))
    holder = await gate.acquire()
    w1 = _spawn(gate)
    w2 = _spawn(gate)
    await _settle()
    w1.cancel()
    await _settle()
    assert gate.queue_depth() == 1  # 取消的 waiter 已從佇列移除

    gate.release(holder)
    t2 = await asyncio.wait_for(w2, 1)
    assert gate.in_flight() == 1
    gate.release(t2)
    assert gate.in_flight() == 0


async def test_single_timer_handle_is_replaced_not_stacked(
    fake_clock: list[float],
) -> None:
    gate = RateGate(_policy(window_limit=1))
    state = gate._state()
    first = await _acq(gate)
    waiter = _spawn(gate)
    await _settle()
    h1 = state._timer
    assert h1 is not None
    state.pump()
    assert h1.cancelled()
    assert state._timer is not None and state._timer is not h1
    waiter.cancel()
    await _settle()
    state.pump()
    assert state._timer is None  # 無等待者即不留計時器
    gate.release(first)


# ---------------------------------------------------------------------------
# 逾時、取消、佇列滿
# ---------------------------------------------------------------------------
async def test_timeout_removes_waiter_and_counts() -> None:
    gate = RateGate(_policy(max_concurrency=1, max_wait_interactive=0.05))
    holder = await gate.acquire()
    with pytest.raises(RateGateTimeoutError) as ei:
        await _acq(gate, True)
    assert ei.value.source == "t"
    assert gate.queue_depth() == 0
    assert gate.in_flight() == 1  # 逾時者不佔名額
    gate.release(holder)
    # 之後的請求不受殘留 waiter 影響
    t = await asyncio.wait_for(_acq(gate, True), 1)
    gate.release(t)
    assert gate.drain_stats().timeouts == 1


async def test_cancel_in_same_turn_as_grant_does_not_leak_slot() -> None:
    gate = RateGate(_policy(max_concurrency=1))
    holder = await gate.acquire()
    w = _spawn(gate)
    await _settle()
    assert gate.queue_depth() == 1

    # release 使 pump 同步核發給 w（in_flight 立即 +1），但 w 尚未被喚醒就被取消
    gate.release(holder)
    assert gate.in_flight() == 1
    w.cancel()
    with pytest.raises(asyncio.CancelledError):
        await w
    assert gate.in_flight() == 0  # 已核發的名額被歸還
    # 閘門仍可正常使用
    t = await asyncio.wait_for(gate.acquire(), 1)
    gate.release(t)
    assert gate.in_flight() == 0


async def test_queue_full_rejects_and_counts() -> None:
    gate = RateGate(_policy(max_concurrency=1, max_queue_depth_background=1))
    holder = await gate.acquire()
    w = _spawn(gate)
    await _settle()
    with pytest.raises(RateGateQueueFullError):
        await gate.acquire()
    assert gate.drain_stats().queue_full == 1
    gate.release(holder)
    gate.release(await asyncio.wait_for(w, 1))


# ---------------------------------------------------------------------------
# 冷卻
# ---------------------------------------------------------------------------
async def test_cooldown_fast_fail_spends_no_quota(fake_clock: list[float]) -> None:
    gate = RateGate(_policy(cooldown_initial=10, cooldown_max=100))
    state = gate._state()
    gate.trip()
    assert gate.in_cooldown()
    with pytest.raises(RateGateCooldownError) as ei:
        await _acq(gate, True)
    assert "fast-circuit to fallback" in str(ei.value)
    with pytest.raises(RateGateCooldownError):
        await _acq(gate, False)  # 預設背景也快速熔斷
    assert len(state.window) == 0
    assert gate.in_flight() == 0
    assert gate.drain_stats().granted == 0


async def test_cooldown_message_matches_legacy_finnhub_matcher() -> None:
    gate = rate_gate.get_gate("finnhub")
    gate.trip()
    with pytest.raises(RateGateCooldownError) as ei:
        with mark_interactive_request():
            await gate.acquire()
    assert str(ei.value) == "Finnhub rate limited, fast-circuit to fallback"


async def test_background_waits_through_cooldown_when_not_fail_fast(
    fake_clock: list[float],
) -> None:
    gate = RateGate(
        _policy(background_fail_fast_in_cooldown=False, cooldown_initial=10)
    )
    state = gate._state()
    gate.trip()
    bg = _spawn(gate)
    await _settle()
    assert not bg.done()
    assert gate.queue_depth("background") == 1
    with pytest.raises(RateGateCooldownError):
        await _acq(gate, True)  # 互動仍快速熔斷

    fake_clock[0] += 11
    state.pump()
    gate.release(await asyncio.wait_for(bg, 1))


async def test_trip_fails_queued_waiters(fake_clock: list[float]) -> None:
    gate = RateGate(_policy(max_concurrency=1))
    holder = await gate.acquire()
    inter = _spawn(gate, True)
    bg = _spawn(gate)
    await _settle()
    gate.trip(30)
    for w in (inter, bg):
        with pytest.raises(RateGateCooldownError):
            await asyncio.wait_for(w, 1)
    assert gate.queue_depth() == 0
    gate.release(holder)


async def test_backoff_escalation_and_mark_ok_reset(fake_clock: list[float]) -> None:
    gate = RateGate(_policy(cooldown_initial=10, cooldown_max=35))
    assert gate.trip() == pytest.approx(10)
    # 冷卻中再 trip：不升級倍數
    fake_clock[0] += 3
    assert gate.trip() == pytest.approx(7)
    assert gate.trip(retry_after=2) == pytest.approx(7)  # 較短的 Retry-After 不縮短
    assert gate.trip(retry_after=20) == pytest.approx(20)  # 較長的延長
    # 冷卻結束後再 trip：10→20→35(上限)
    fake_clock[0] += 21
    assert not gate.in_cooldown()
    assert gate.trip() == pytest.approx(20)
    fake_clock[0] += 21
    assert gate.trip() == pytest.approx(35)
    fake_clock[0] += 36
    assert gate.trip() == pytest.approx(35)  # 上限
    # 在冷卻中 mark_ok 不重置；請求早於最近 trip 也不重置
    fake_clock[0] += 36
    gate.mark_ok(fake_clock[0] - 100)
    assert gate.trip() == pytest.approx(35)
    fake_clock[0] += 36
    gate.mark_ok(fake_clock[0])  # 冷卻結束後送出的成功請求 → 重置
    assert gate.trip() == pytest.approx(10)


async def test_retry_after_capped_and_overrides_backoff(
    fake_clock: list[float],
) -> None:
    gate = RateGate(_policy(cooldown_initial=10, cooldown_max=50))
    assert gate.trip(retry_after=500) == pytest.approx(50)
    fake_clock[0] += 51
    assert gate.trip(retry_after=7) == pytest.approx(7)
    fake_clock[0] += 8
    # Retry-After 不影響退避計數：無 Retry-After 時自初始值起算
    assert gate.trip() == pytest.approx(10)


async def test_trip_from_other_thread_loop_wakes_main_loop_waiters() -> None:
    gate = RateGate(_policy(max_concurrency=1))
    holder = await gate.acquire()
    waiter = _spawn(gate, True)
    await _settle()
    assert gate.queue_depth("interactive") == 1

    def _other_loop() -> None:
        async def _t() -> None:
            gate.trip(30)

        asyncio.run(_t())

    th = threading.Thread(target=_other_loop)
    th.start()
    with pytest.raises(RateGateCooldownError):
        await asyncio.wait_for(waiter, 2)
    th.join(2)
    assert gate.queue_depth() == 0
    gate.release(holder)


async def test_trip_ignores_closed_loops() -> None:
    gate = RateGate(_policy())
    loops: list[asyncio.AbstractEventLoop] = []  # 持有參照，狀態才不會被 GC 掉

    def _other_thread() -> None:
        async def _use() -> None:
            gate.release(await gate.acquire())

        loop = asyncio.new_event_loop()
        loops.append(loop)
        loop.run_until_complete(_use())
        loop.close()

    th = threading.Thread(target=_other_thread)
    th.start()
    th.join(5)
    assert loops and loops[0].is_closed()
    assert len(gate._states) == 1  # 已註冊但 loop 已關閉
    gate.trip(1)  # 不得因已關閉的 loop 拋例外
    assert gate.in_cooldown()


# ---------------------------------------------------------------------------
# 觀測與模組層
# ---------------------------------------------------------------------------
async def test_drain_stats_reports_wait_and_resets(fake_clock: list[float]) -> None:
    gate = RateGate(_policy(max_concurrency=1))
    a = await gate.acquire()
    b = _spawn(gate)
    await _settle()
    fake_clock[0] += 3
    gate.release(a)
    tb = await asyncio.wait_for(b, 1)
    gate.release(tb)
    st = gate.drain_stats()
    assert st.granted == 2
    assert st.wait_max == pytest.approx(3.0)
    assert st.wait_p95 == pytest.approx(3.0)
    assert st.depth_peak == 1
    assert gate.drain_stats().is_empty()


async def test_slot_context_manager_releases_on_error() -> None:
    gate = RateGate(_policy(max_concurrency=1))
    with pytest.raises(ValueError):
        async with gate.slot():
            assert gate.in_flight() == 1
            raise ValueError("boom")
    assert gate.in_flight() == 0


def test_module_level_registry_and_reset() -> None:
    g = rate_gate.get_gate("finnhub")
    assert g is rate_gate.get_gate("finnhub")
    assert g.policy.window_limit == 50
    assert g.policy.interactive_reserve == 15
    assert g.policy.background_fail_fast_in_cooldown is False
    g.trip()
    assert g.in_cooldown()
    rate_gate.reset_for_tests()
    assert not rate_gate.get_gate("finnhub").in_cooldown()
    assert rate_gate.drain_stats() == {}


def test_drain_stats_only_lists_active_gates() -> None:
    rate_gate.get_gate("yahoo")
    rate_gate.get_gate("sec").trip()
    stats = rate_gate.drain_stats()
    assert set(stats) == {"sec"}
    assert stats["sec"].cooldowns == 1


def test_all_policies_valid_and_background_fits_reserve() -> None:
    for name, p in rate_gate.POLICIES.items():
        assert p.name == name
        assert p.interactive_reserve < p.window_limit
        assert p.bg_concurrency <= p.max_concurrency
    # Finnhub：互動保留 15、背景最多 35；Yahoo 背景仍 ≤ 30/分
    fin = rate_gate.POLICIES["finnhub"]
    assert fin.window_limit - fin.interactive_reserve == 35
    yh = rate_gate.POLICIES["yahoo"]
    assert yh.window_limit - yh.interactive_reserve == 30


def test_interactive_contextvar_is_shared_object_with_core() -> None:
    from services.market_data_service import _core

    assert _core._is_interactive_request is rate_gate._is_interactive_request
    assert _core.mark_interactive_request is rate_gate.mark_interactive_request
    assert _core.parse_retry_after is rate_gate.parse_retry_after


def test_parse_retry_after() -> None:
    parse: Callable[[Any], float | None] = rate_gate.parse_retry_after
    assert parse({"Retry-After": "12"}) == 12.0
    assert parse({"retry-after": "3.5"}) == 3.5
    assert parse({"Retry-After": "Wed, 21 Oct 2026 07:28:00 GMT"}) is None
    assert parse({}) is None
    assert parse(None) is None


def test_invalid_policy_rejected() -> None:
    with pytest.raises(ValueError):
        GatePolicy(
            name="x",
            window_limit=3,
            window_seconds=1,
            max_concurrency=1,
            interactive_reserve=3,
        )
    with pytest.raises(ValueError):
        GatePolicy(
            name="x",
            window_limit=3,
            window_seconds=1,
            max_concurrency=1,
            background_concurrency=2,
        )


def test_failure_log_level_downgrades_gate_errors_only() -> None:
    """閘門主動拒絕（冷卻／逾時／滿載）記 WARNING；其他例外維持 ERROR。"""
    import logging

    from services.rate_gate import (
        RateGateCooldownError,
        RateGateQueueFullError,
        RateGateTimeoutError,
        failure_log_level,
    )

    for exc_cls in (
        RateGateCooldownError,
        RateGateTimeoutError,
        RateGateQueueFullError,
    ):
        assert failure_log_level(exc_cls("sec", "x")) == logging.WARNING
    assert failure_log_level(RuntimeError("boom")) == logging.ERROR
