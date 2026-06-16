import asyncio
import logging
from typing import Dict, Any, Callable, Coroutine

logger = logging.getLogger(__name__)


class SingleFlightManager:
    _active_tasks: Dict[str, asyncio.Task] = {}
    _lock = asyncio.Lock()

    @classmethod
    async def run(
        cls,
        key: str,
        coro_func: Callable[..., Coroutine[Any, Any, Any]],
        *args,
        **kwargs,
    ) -> Any:
        """
        Runs the coroutine for the given key. If a task with the same key is already running,
        awaits it instead of starting a new one.
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
                def cleanup(t):
                    async def do_cleanup():
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

        return await task
