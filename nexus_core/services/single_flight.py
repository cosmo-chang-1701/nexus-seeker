from typing import Any, Optional
import asyncio
import logging
from typing import Dict, Callable, Coroutine

logger = logging.getLogger(__name__)


class SingleFlightManager:
    _active_tasks: Dict[str, asyncio.Task] = {}
    _lock = asyncio.Lock()

    @classmethod
    async def run(  # type: ignore
        cls,
        key: str,
        coro_func: Callable[..., Coroutine[Any, Any, Any]],
        *args,
        timeout: Optional[float] = None,
        **kwargs,
    ) -> Any:
        """
        Runs the coroutine for the given key. If a task with the same key is already running,
        awaits it instead of starting a new one.

        timeout: 若提供，僅限制「這次呼叫」等待共享任務的時間 (asyncio.TimeoutError)，
        不會取消底層共享任務本身 —— 其他仍在等待同一個 key 的呼叫者、以及該任務
        完成後寫回快取的動作，都不受這次逾時影響，繼續在背景執行。預設為 None
        （沿用既有行為：無限期等待），刻意不強制套用到既有呼叫端，僅供需要有界
        等待時間的呼叫端（例如受 Discord 互動逾時限制的路徑）自行選用。
        """
        async with cls._lock:
            if key in cls._active_tasks:
                logger.info(
                    f"SingleFlightManager: Coalescing concurrent task for key: {key}"
                )
                task = cls._active_tasks[key]
            else:
                logger.info(f"SingleFlightManager: Creating new task for key: {key}")
                # Create a task for the coroutine function
                task = asyncio.create_task(coro_func(*args, **kwargs))
                cls._active_tasks[key] = task

                # Cleanup the task from active dict when it finishes
                def cleanup(t: Any) -> None:
                    async def do_cleanup() -> None:
                        async with cls._lock:
                            if cls._active_tasks.get(key) is t:
                                cls._active_tasks.pop(key, None)
                                logger.info(
                                    f"SingleFlightManager: Cleaned up active task for key: {key}"
                                )

                    try:
                        loop = asyncio.get_running_loop()
                        loop.create_task(do_cleanup())
                    except Exception as e:
                        logger.error(f"Error scheduling SingleFlight task cleanup: {e}")

                task.add_done_callback(cleanup)

        if timeout is not None:
            return await asyncio.wait_for(asyncio.shield(task), timeout=timeout)
        return await task
