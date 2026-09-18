from typing import Any, Optional
import asyncio
import logging
from typing import Dict, Callable, Coroutine

logger = logging.getLogger(__name__)


class SingleFlightManager:
    """把「同一瞬間對同一 key 的多個併發呼叫」合併成一次實際執行。

    存在理由：所有 TTL 快取的寫入都發生在網路 ``await`` **之後**，因此
    「查快取 → 發請求 → 寫快取」之間隔著一整段等待。在 t=0 一起建立的多個 task
    會雙雙 miss 快取並發出完全相同的請求。**TTL 快取消除的是「跨輪次」重複，
    single-flight 消除的是「同輪次併發」重複，兩者不互相取代。**

    呼叫端範例見 ``services/market_data_service/`` 的 ``get_history_df`` /
    ``get_quote`` / ``get_all_option_expiries`` / ``get_option_chain``，以及
    ``market_analysis/index_microstructure.py``。
    """

    _active_tasks: Dict[str, "asyncio.Task[Any]"] = {}

    # 刻意**沒有** asyncio.Lock：`run()` 的臨界區（查表 → 建立 task → 寫回表）
    # 內部完全沒有 await，在單執行緒 event loop 上已經是不可分割的操作，鎖提供
    # 不了額外保護。早期版本的 `async with cls._lock` 反而帶來兩個問題：
    #   1. 類別層級的 `asyncio.Lock` 一旦真的發生競爭就會綁定到當時的 event
    #      loop，之後在別的 loop（單元測試每個 case 各自建 loop）使用會拋
    #      「is bound to a different event loop」。
    #   2. 它讓清理動作只能寫成 async（`async with` 需要 coroutine），因而逼出
    #      `loop.create_task(do_cleanup())` 這種延後一輪才生效的清理——正是
    #      「已完成但尚未清掉的 task 被後續呼叫取用而回傳上一次結果」的來源。

    @classmethod
    def _discard(cls, key: str, task: "asyncio.Task[Any]") -> None:
        """``done_callback``：同步把自己從 active 表移除。

        刻意是同步函式（而非再排一個 cleanup task）：`add_done_callback` 本身已
        由 event loop 以 `call_soon` 呼叫，在其中再排一層 task 只是把清理又延後
        一輪，毫無益處。
        """
        if cls._active_tasks.get(key) is task:
            del cls._active_tasks[key]
            logger.debug(f"SingleFlight[{key}] 共享任務已清理")

        # 主動取走例外：所有呼叫端都被取消時沒人會 await 這個 task，asyncio 會在
        # GC 期印出 "Task exception was never retrieved"。取走後改以 debug 記錄，
        # 既消除噪音又保留可觀測性。
        if not task.cancelled():
            exc = task.exception()
            if exc is not None:
                logger.debug(f"SingleFlight[{key}] 共享任務以例外結束: {exc!r}")

    @classmethod
    def _reusable(cls, key: str) -> Optional["asyncio.Task[Any]"]:
        """回傳可共乘的進行中任務；不可共乘時順手清掉殘跡並回 ``None``。

        兩種「表中有紀錄但不可共乘」的情形：

        * **已完成**：`done_callback` 是 `call_soon` 排程的，任務完成到回呼執行
          之間有一個 loop 迭代的空窗。此處顯式排除已完成的任務，讓正確性不依賴
          回呼時序——否則該空窗內的後續呼叫會拿到**上一次**的結果（快取已被刻意
          清除或繞過時就是實質錯誤）。
        * **屬於別的 event loop**：跨 loop 的 Future 不可 await。單元測試每個
          case 各自建 loop，前一個 loop 若異常中止會留下殘跡；此處直接忽略並
          重新執行，而不是拋出難以追查的錯誤。
        """
        task = cls._active_tasks.get(key)
        if task is None:
            return None

        if task.done():
            if cls._active_tasks.get(key) is task:
                del cls._active_tasks[key]
            return None

        if task.get_loop() is not asyncio.get_running_loop():
            logger.warning(
                f"SingleFlight[{key}] 表中殘留屬於其他 event loop 的任務，已忽略並重新執行"
            )
            if cls._active_tasks.get(key) is task:
                del cls._active_tasks[key]
            return None

        return task

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

        共享任務**一律**以 `asyncio.shield` 包裹後等待：任一呼叫端自身被取消時，
        不得連帶取消共享任務——那會波及其他仍在等待同一個 key 的呼叫端，也會讓
        已經付出的網路成本白費（任務完成後寫回快取的動作同樣值得保留）。因此
        「取消某個呼叫端」的語意是「我不等了」，而非「中止這件事」。

        timeout: 若提供，僅限制「這次呼叫」等待共享任務的時間 (asyncio.TimeoutError)，
        不會取消底層共享任務本身 —— 其他仍在等待同一個 key 的呼叫者、以及該任務
        完成後寫回快取的動作，都不受這次逾時影響，繼續在背景執行。預設為 None
        （無限期等待），刻意不強制套用到既有呼叫端，僅供需要有界等待時間的呼叫端
        （例如受 Discord 互動逾時限制的路徑）自行選用。
        """
        task = cls._reusable(key)
        if task is not None:
            logger.debug(f"SingleFlight[{key}] 併入進行中的共享任務")
        else:
            logger.debug(f"SingleFlight[{key}] 建立新的共享任務")
            task = asyncio.create_task(coro_func(*args, **kwargs))
            cls._active_tasks[key] = task
            task.add_done_callback(lambda t: cls._discard(key, t))

        if timeout is not None:
            return await asyncio.wait_for(asyncio.shield(task), timeout=timeout)
        return await asyncio.shield(task)
