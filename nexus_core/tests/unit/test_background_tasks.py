"""背景任務葉模組：執行中保留強參照，完成後移除，且不吞例外。"""

import asyncio
import gc
import weakref
from typing import Any, Iterator

import pytest

from services import background_tasks as bt
from services.background_tasks import spawn_background
from tests.unit.network_guard import NetworkGuard


@pytest.fixture(autouse=True)
def _no_outbound_network(monkeypatch: pytest.MonkeyPatch) -> Iterator[NetworkGuard]:
    """攔截所有對外連線；即使呼叫端吞掉例外，teardown 仍會讓測試失敗。"""
    guard = NetworkGuard()
    guard.install(monkeypatch)
    yield guard
    guard.assert_clean()


async def _drain() -> None:
    """讓背景任務與其完成回呼跑完。"""
    for _ in range(5):
        await asyncio.sleep(0)


@pytest.mark.asyncio
async def test_pending_task_survives_gc_after_caller_drops_reference() -> None:
    loop = asyncio.get_running_loop()
    waiters: list[weakref.ref["asyncio.Future[None]"]] = []
    finished: list[bool] = []

    async def _work() -> None:
        # 外部只持弱參照：沒有強參照保存時，任務與 future 的循環會被 GC 回收
        fut: asyncio.Future[None] = loop.create_future()
        waiters.append(weakref.ref(fut))
        await fut
        finished.append(True)

    task_ref = weakref.ref(spawn_background(_work()))
    await _drain()
    gc.collect()

    assert task_ref() in bt._BACKGROUND_TASKS
    waiter = waiters[0]()
    assert waiter is not None
    waiter.set_result(None)
    await _drain()

    assert finished == [True]
    task = task_ref()
    assert task is None or task not in bt._BACKGROUND_TASKS


@pytest.mark.asyncio
async def test_return_value_available_and_task_discarded() -> None:
    async def _work() -> int:
        return 42

    task = spawn_background(_work())
    assert task in bt._BACKGROUND_TASKS
    assert await task == 42
    await _drain()
    assert task not in bt._BACKGROUND_TASKS


@pytest.mark.asyncio
async def test_unretrieved_exception_still_reported() -> None:
    loop = asyncio.get_running_loop()
    reported: list[tuple[Any, Any]] = []
    boom = RuntimeError("背景任務失敗")

    async def _work() -> None:
        raise boom

    previous = loop.get_exception_handler()
    loop.set_exception_handler(
        lambda _, ctx: reported.append((ctx.get("message"), ctx.get("exception")))
    )
    try:
        task_ref = weakref.ref(spawn_background(_work()))
        await _drain()
        gc.collect()
    finally:
        loop.set_exception_handler(previous)

    assert task_ref() is None
    assert ("Task exception was never retrieved", boom) in reported
