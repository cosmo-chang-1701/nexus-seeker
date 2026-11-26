"""services/background_tasks.py

fire-and-forget 背景任務的強參照保存（僅用 stdlib 的葉模組，避免循環匯入）。

event loop 對 task 只持有弱參照；呼叫端丟棄 `asyncio.create_task()` 的回傳值時，
任務可能在執行中被 GC 回收。
"""

import asyncio
from typing import Any, Coroutine, TypeVar

T = TypeVar("T")

_BACKGROUND_TASKS: set[asyncio.Task[Any]] = set()


def spawn_background(coro: Coroutine[Any, Any, T]) -> asyncio.Task[T]:
    """建立背景任務並保留強參照至完成為止。

    不吞例外也不記錄日誌：完成時的回呼只移除參照，不呼叫 `exception()`／
    `result()`，因此未被取回的例外仍由 asyncio 在 GC 時回報。
    """
    task = asyncio.create_task(coro)
    _BACKGROUND_TASKS.add(task)
    task.add_done_callback(_BACKGROUND_TASKS.discard)
    return task
