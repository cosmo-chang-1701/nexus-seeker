import sqlite3
import logging
import asyncio
import threading
from typing import Any, Optional
import config

logger = logging.getLogger(__name__)


def get_read_connection() -> sqlite3.Connection:
    """
    Returns a read-only database connection with WAL mode, normal sync, and 15s timeout.
    """
    conn = sqlite3.connect(config.DB_NAME, timeout=15.0)
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("PRAGMA synchronous=NORMAL;")
    return conn


class DatabaseWriteQueue:
    _queue: Optional[asyncio.Queue] = None
    _loop: Optional[asyncio.AbstractEventLoop] = None
    _loop_thread: Optional[threading.Thread] = None
    _worker_task: Optional[asyncio.Task] = None
    _running: bool = False
    _lock = threading.Lock()

    @classmethod
    def initialize(cls, loop: asyncio.AbstractEventLoop):
        with cls._lock:
            cls._loop = loop
            cls._loop_thread = threading.current_thread()
            cls._queue = asyncio.Queue()
            cls._running = True
            cls._worker_task = loop.create_task(cls._worker_loop())
            logger.info(
                "DatabaseWriteQueue background worker started in the event loop."
            )

    @classmethod
    def is_active(cls) -> bool:
        with cls._lock:
            return cls._running and cls._loop is not None and cls._queue is not None

    @classmethod
    async def stop_worker(cls):
        with cls._lock:
            cls._running = False
            task = cls._worker_task
        if task:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
            logger.info("DatabaseWriteQueue background worker stopped.")

    @classmethod
    async def put_task(cls, task_type: str, data: tuple, commit: bool = True) -> Any:
        """
        Async interface to put a task into the queue and await completion.
        Used when called directly from async contexts.
        """
        if not cls.is_active():
            # Fallback to direct write if queue not active (e.g. CLI, tests)
            loop = asyncio.get_running_loop()
            return await loop.run_in_executor(
                None, cls._execute_direct_write, task_type, data, commit
            )

        loop = asyncio.get_running_loop()
        future = loop.create_future()
        # Put task: (task_type, data, commit, future, None)
        assert cls._queue is not None
        await cls._queue.put((task_type, data, commit, future, None))
        return await future

    @classmethod
    def put_task_sync(cls, task_type: str, data: tuple, commit: bool = True) -> Any:
        """
        Thread-safe sync interface to put a task into the queue and block waiting for completion.
        Used when called from sync contexts (e.g. worker threads via asyncio.to_thread, CLI, tests).
        """
        # 1. If queue is not active or loop is not running, write directly
        if not cls.is_active():
            return cls._execute_direct_write(task_type, data, commit)

        # 2. Check if we are in the main event loop thread. If so, throw exception to prevent thread blocking!
        if threading.current_thread() is cls._loop_thread:
            raise RuntimeError(
                f"Sync database write ({task_type}) called from main event loop thread. "
                "This blocks the event loop and is strictly prohibited to prevent latency. "
                "Please refactor the caller to use execute_write_async or await put_task directly."
            )

        # 3. We are in a worker thread. We can safely block using threading.Event.
        event = threading.Event()
        result = {"success": False, "data": None, "error": None}

        assert cls._queue is not None
        assert cls._loop is not None

        def enqueue():
            assert cls._queue is not None
            cls._queue.put_nowait((task_type, data, commit, None, (event, result)))

        cls._loop.call_soon_threadsafe(enqueue)
        event.wait()

        err = result["error"]
        if err is not None:
            if isinstance(err, Exception):
                raise err
            else:
                raise RuntimeError(str(err))
        return result["data"]

    @classmethod
    async def _worker_loop(cls):
        conn = None
        try:
            conn = sqlite3.connect(config.DB_NAME, timeout=30.0)
            conn.execute("PRAGMA journal_mode=WAL;")
            conn.execute("PRAGMA synchronous=NORMAL;")
            logger.info("DatabaseWriteQueue worker connection established (WAL mode).")

            while cls._running:
                try:
                    task = await cls._queue.get()
                except asyncio.CancelledError:
                    break

                task_type, data, commit, future, sync_event_payload = task

                try:
                    res = await cls._process_task(conn, task_type, data, commit)

                    if future:
                        if not future.cancelled():
                            future.set_result(res)
                    elif sync_event_payload:
                        event, result = sync_event_payload
                        result["success"] = True
                        result["data"] = res
                        event.set()
                except Exception as e:
                    logger.error(f"Error processing write task {task_type}: {e}")
                    try:
                        conn.rollback()
                    except Exception as rb_err:
                        logger.error(f"Rollback failed: {rb_err}")

                    if future:
                        if not future.cancelled():
                            future.set_exception(e)
                    elif sync_event_payload:
                        event, result = sync_event_payload
                        result["success"] = False
                        result["error"] = e
                        event.set()
                finally:
                    cls._queue.task_done()
        except Exception as e:
            logger.critical(
                f"DatabaseWriteQueue worker loop encountered critical error: {e}"
            )
        finally:
            if conn:
                conn.close()
                logger.info("DatabaseWriteQueue worker connection closed.")

    @classmethod
    async def _process_task(
        cls, conn: sqlite3.Connection, task_type: str, data: tuple, commit: bool
    ) -> Any:
        cursor = conn.cursor()
        if task_type == "save_historical_iv":
            # Task 4 self-healing and fallback logic!
            symbol, iv, date_str = data
            import math

            if iv is None or (isinstance(iv, float) and math.isnan(iv)):
                logger.warning(
                    f"[{symbol}] save_historical_iv received invalid IV ({iv}). Triggering fallback/self-healing."
                )

                # Fallback 1: Yesterday's closing IV
                try:
                    cursor.execute(
                        "SELECT iv FROM historical_iv WHERE symbol = ? ORDER BY date DESC LIMIT 1",
                        (symbol,),
                    )
                    row = cursor.fetchone()
                    if (
                        row
                        and row[0] is not None
                        and not (isinstance(row[0], float) and math.isnan(row[0]))
                    ):
                        iv = row[0]
                        logger.info(
                            f"[{symbol}] Fallback 1: Loaded last closing IV ({iv}) from historical_iv."
                        )
                except Exception as db_err:
                    logger.error(f"[{symbol}] Fallback 1 query failed: {db_err}")

                # Fallback 2: 30-day Historical Volatility (HV)
                if iv is None or (isinstance(iv, float) and math.isnan(iv)):
                    try:
                        from services.market_data_service import get_history_df
                        import pandas as pd
                        import numpy as np

                        # Fetch 1 month historical data asynchronously
                        df_temp = await get_history_df(symbol, period="1mo")
                        if not df_temp.empty and len(df_temp) >= 2:
                            df_temp["Log_Ret"] = np.log(
                                df_temp["Close"] / df_temp["Close"].shift(1)
                            )
                            hv = float(df_temp["Log_Ret"].std() * np.sqrt(252))
                            if not pd.isna(hv) and hv > 0:
                                iv = hv
                                logger.info(
                                    f"[{symbol}] Fallback 2: Calculated 30-day HV ({iv}) as fallback."
                                )
                    except Exception as hv_err:
                        logger.error(
                            f"[{symbol}] Fallback 2 calculation failed: {hv_err}"
                        )

            if iv is None or (isinstance(iv, float) and math.isnan(iv)):
                logger.warning(
                    f"⚠️ [{symbol}] All IV fallbacks failed. Skipping writing to historical_iv to prevent NOT NULL constraint error."
                )
                return False

            # Execute save_historical_iv query
            cursor.execute(
                """
                INSERT OR REPLACE INTO historical_iv (symbol, iv, date)
                VALUES (?, ?, ?)
                """,
                (symbol, iv, date_str),
            )
            if commit:
                conn.commit()
            return True

        elif task_type == "sql":
            query, params = data
            cursor.execute(query, params)
            if commit:
                conn.commit()
            return cursor.lastrowid or cursor.rowcount or True

        else:
            raise ValueError(f"Unknown task type: {task_type}")

    @classmethod
    def _execute_direct_write(
        cls, task_type: str, data: tuple, commit: bool = True
    ) -> Any:
        """
        Direct write fallback when queue is not running.
        Crucial for migrations, tests, and CLI tool stability.
        """
        conn = sqlite3.connect(config.DB_NAME, timeout=15.0)
        conn.execute("PRAGMA journal_mode=WAL;")
        conn.execute("PRAGMA synchronous=NORMAL;")
        cursor = conn.cursor()
        try:
            if task_type == "save_historical_iv":
                symbol, iv, date_str = data
                import math

                if iv is None or (isinstance(iv, float) and math.isnan(iv)):
                    # Fallback 1: DB
                    cursor.execute(
                        "SELECT iv FROM historical_iv WHERE symbol = ? ORDER BY date DESC LIMIT 1",
                        (symbol,),
                    )
                    row = cursor.fetchone()
                    if (
                        row
                        and row[0] is not None
                        and not (isinstance(row[0], float) and math.isnan(row[0]))
                    ):
                        iv = row[0]
                if iv is None or (isinstance(iv, float) and math.isnan(iv)):
                    # Try fallback 2 (Historical Volatility) synchronously
                    try:
                        from services.market_data_service import get_history_df
                        import pandas as pd
                        import numpy as np

                        loop = None
                        try:
                            loop = asyncio.get_event_loop()
                        except RuntimeError:
                            pass

                        if loop and loop.is_running():
                            future = asyncio.run_coroutine_threadsafe(
                                get_history_df(symbol, period="1mo"), loop
                            )
                            df_temp = future.result(timeout=10)
                        else:
                            df_temp = asyncio.run(get_history_df(symbol, period="1mo"))

                        if not df_temp.empty and len(df_temp) >= 2:
                            df_temp["Log_Ret"] = np.log(
                                df_temp["Close"] / df_temp["Close"].shift(1)
                            )
                            hv = float(df_temp["Log_Ret"].std() * np.sqrt(252))
                            if not pd.isna(hv) and hv > 0:
                                iv = hv
                    except Exception as e:
                        logger.error(f"Direct write fallback 2 failed: {e}")

                if iv is None or (isinstance(iv, float) and math.isnan(iv)):
                    logger.warning(
                        f"⚠️ [{symbol}] All IV fallbacks failed in direct write. Skipping."
                    )
                    return False

                cursor.execute(
                    """
                    INSERT OR REPLACE INTO historical_iv (symbol, iv, date)
                    VALUES (?, ?, ?)
                    """,
                    (symbol, iv, date_str),
                )
                if commit:
                    conn.commit()
                return True

            elif task_type == "sql":
                query, params = data
                cursor.execute(query, params)
                if commit:
                    conn.commit()
                return cursor.lastrowid or cursor.rowcount or True
        except Exception as e:
            conn.rollback()
            raise e
        finally:
            conn.close()


def execute_write(query: str, params: tuple = (), commit: bool = True) -> Any:
    """
    Synchronous entry point for all database writes.
    """
    return DatabaseWriteQueue.put_task_sync("sql", (query, params), commit)


async def execute_write_async(
    query: str, params: tuple = (), commit: bool = True
) -> Any:
    """
    Asynchronous entry point for all database writes.
    """
    return await DatabaseWriteQueue.put_task("sql", (query, params), commit)
