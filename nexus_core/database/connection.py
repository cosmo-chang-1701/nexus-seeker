from typing import Any, NamedTuple, Optional, Sequence
import sqlite3
import logging
import asyncio
import queue
import random
import threading
import time
import config

logger = logging.getLogger(__name__)


# ==========================================================================
# 連線層統一設定
# ==========================================================================
# 單一 busy timeout 旋鈕，取代先前散落於各處的 5s（預設）/ 15s / 30s 三種值。
# 一般連線（讀取與 CLI 直寫）用這個值。
_BUSY_TIMEOUT_MS = 15_000

# 寫入 worker 的單次等待上限刻意較短：它自己還有下面的退避重試，
# 單次 15 秒 × 多次重試會讓一筆寫入的最壞延遲長到沒有意義。
_WRITER_BUSY_TIMEOUT_MS = 5_000

# SQLITE_BUSY 退避重試。藍綠部署期間會有兩個容器並存掛載同一個 DB volume，
# 「單一寫入佇列」在該窗口內必然失效，因此跨程序的鎖競爭一律以退避重試吸收，
# 而不是讓它冒泡成 `database is locked` 錯誤。
_LOCK_RETRY_ATTEMPTS = 4
_LOCK_RETRY_BASE_DELAY = 0.2

# 佇列滿載時的入列等待上限；以及 put_task_sync 等待結果的上限。
# 後者必須大於「最壞情況的重試總時長」，但仍要有界——舊版的 event.wait() 沒有
# timeout，worker 一旦死亡，所有 to_thread 呼叫端會永久卡死。
_QUEUE_PUT_TIMEOUT = 30.0
_SYNC_WAIT_TIMEOUT = 90.0
_WORKER_JOIN_TIMEOUT = 30.0

# 佇列未啟用時（migration / CLI / 測試 / 關機競態）的直寫路徑仍必須維持
# 「程序內單一寫入者」的不變式。少了這道鎖，關機時 `_running` 一翻為 False，
# 所有仍在途中的寫入會同時散開成數十條各自獨立的連線互搶寫入鎖。
_direct_write_lock = threading.Lock()


def _apply_connection_pragmas(
    conn: sqlite3.Connection, timeout_ms: int
) -> sqlite3.Connection:
    """套用每條連線都需要的 PRAGMA。

    刻意**不**在這裡設定 `journal_mode=WAL`：WAL 寫在資料庫檔頭、是持久設定，
    由 `database/core.py::run_migrations()` 於啟動時設定一次即可。熱路徑（例如
    雷達掃描）每個標的會開關數十條連線，省下這條語句是實質收益。
    """
    # nosemgrep: python.lang.security.audit.formatted-sql-query.formatted-sql-query
    conn.execute(f"PRAGMA busy_timeout={int(timeout_ms)};")
    conn.execute("PRAGMA synchronous=NORMAL;")
    return conn


def connect_db(timeout_ms: int = _BUSY_TIMEOUT_MS) -> sqlite3.Connection:
    """唯一的 SQLite 連線工廠。所有連線一律經過這裡，確保 busy timeout 一致。"""
    conn = sqlite3.connect(config.DB_NAME, timeout=timeout_ms / 1000.0)
    return _apply_connection_pragmas(conn, timeout_ms)


def get_read_connection() -> sqlite3.Connection:
    """取得一條讀取用連線（WAL 下讀取不阻塞寫入）。

    保留原名原簽章以維持既有 19 處呼叫端不變。注意它並非真正的 read-only 連線
    （未帶 `mode=ro`），命名為 read 是表達用途而非強制。
    """
    return connect_db()


class _WriteTask(NamedTuple):
    task_type: str
    data: tuple
    commit: bool
    # async 呼叫端等待的 future（由其所屬 event loop 以 call_soon_threadsafe 回填）
    future: Optional["asyncio.Future[Any]"]
    # sync 呼叫端等待的 (Event, 結果容器)
    sync_payload: Optional[tuple[threading.Event, dict[str, Any]]]


class DatabaseWriteQueue:
    """單一寫入者佇列。

    ⚠️ 關鍵設計：worker 跑在**專屬的背景執行緒**上，不是 event loop 的 task。
    早期版本用 `loop.create_task(_worker_loop())`，而 `cursor.execute()` /
    `conn.commit()` 是同步 C 呼叫——一旦 SQLite 回 SQLITE_BUSY，busy handler 就在
    Discord gateway 的執行緒上睡滿整個 busy timeout，直接觸發
    「heartbeat blocked for more than N seconds」，逾時後再拋 `database is locked`。
    """

    _queue: Optional["queue.Queue[Optional[_WriteTask]]"] = None
    _loop: Optional[asyncio.AbstractEventLoop] = None
    _loop_thread: Optional[threading.Thread] = None
    _worker_thread: Optional[threading.Thread] = None
    _running: bool = False
    _lock = threading.Lock()

    # ------------------------------------------------------------------
    # 生命週期
    # ------------------------------------------------------------------
    @classmethod
    def initialize(cls, loop: asyncio.AbstractEventLoop, maxsize: int = 1000) -> Any:
        with cls._lock:
            if (
                cls._running
                and cls._worker_thread is not None
                and cls._worker_thread.is_alive()
            ):
                logger.warning("DatabaseWriteQueue 已在執行中，略過重複初始化。")
                return

            cls._loop = loop
            # 記錄 event loop 所在執行緒，供 put_task_sync 的守衛比對。
            cls._loop_thread = threading.current_thread()
            # 有界佇列：>10x 目前實際觀測到的寫入流量峰值，純粹作為未來 bug
            # 造成寫入洪水時的防禦性上限，正常操作不會觸發背壓延遲。
            cls._queue = queue.Queue(maxsize=maxsize)
            cls._running = True
            cls._worker_thread = threading.Thread(
                target=cls._worker_main,
                name="nexus-db-writer",
                daemon=True,
            )
            cls._worker_thread.start()
            logger.info(
                "DatabaseWriteQueue worker 已於獨立執行緒啟動 (nexus-db-writer)，"
                "寫入不再佔用 event loop。"
            )

    @classmethod
    def is_active(cls) -> bool:
        with cls._lock:
            return cls._running and cls._loop is not None and cls._queue is not None

    @classmethod
    async def stop_worker(cls) -> None:
        with cls._lock:
            thread = cls._worker_thread
            q = cls._queue
            was_running = cls._running
            cls._running = False

        if not was_running or thread is None or q is None:
            return

        # 投入 sentinel：worker 會先把 FIFO 中排在它前面的寫入全部排空才退出，
        # 比舊版的 task.cancel() 更不容易掉資料。
        try:
            q.put(None, True, _QUEUE_PUT_TIMEOUT)
        except queue.Full:
            logger.error("DatabaseWriteQueue 關閉時無法投入 sentinel（佇列滿載）。")

        await asyncio.to_thread(thread.join, _WORKER_JOIN_TIMEOUT)
        if thread.is_alive():
            logger.warning(
                f"DatabaseWriteQueue worker 於 {_WORKER_JOIN_TIMEOUT}s 內未結束，放棄等待。"
            )
        else:
            logger.info("DatabaseWriteQueue background worker stopped.")

        with cls._lock:
            cls._worker_thread = None

    # ------------------------------------------------------------------
    # 入列
    # ------------------------------------------------------------------
    @classmethod
    async def put_task(cls, task_type: str, data: tuple, commit: bool = True) -> Any:
        """Async interface to put a task into the queue and await completion."""
        if not cls.is_active():
            # Fallback to direct write if queue not active (e.g. CLI, tests)
            loop = asyncio.get_running_loop()
            return await loop.run_in_executor(
                None, cls._execute_direct_write, task_type, data, commit
            )

        loop = asyncio.get_running_loop()
        future: "asyncio.Future[Any]" = loop.create_future()
        task = _WriteTask(task_type, data, commit, future, None)

        q = cls._queue
        assert q is not None
        try:
            q.put_nowait(task)
        except queue.Full:
            # 佇列滿時做非阻塞背壓：把阻塞式 put 丟到執行緒池，
            # event loop 仍可繼續運轉（其他工作不受影響）。
            await asyncio.to_thread(q.put, task, True, _QUEUE_PUT_TIMEOUT)
        return await future

    @classmethod
    def put_task_sync(cls, task_type: str, data: tuple, commit: bool = True) -> Any:
        """Thread-safe sync interface. 僅供 worker thread（asyncio.to_thread）、
        CLI 與測試使用。"""
        # 1. If queue is not active or loop is not running, write directly
        if not cls.is_active():
            return cls._execute_direct_write(task_type, data, commit)

        # 2. 在 event loop 執行緒上同步寫入會阻塞整個 bot，直接擋下。
        if threading.current_thread() is cls._loop_thread:
            raise RuntimeError(
                f"Sync database write ({task_type}) called from main event loop thread. "
                "This blocks the event loop and is strictly prohibited to prevent latency. "
                "Please refactor the caller to use execute_write_async or await put_task directly."
            )

        # 3. We are in a worker thread. We can safely block using threading.Event.
        event = threading.Event()
        result: dict[str, Any] = {"success": False, "data": None, "error": None}

        q = cls._queue
        assert q is not None
        # queue.Queue 本身就是執行緒安全的，不需要再繞 run_coroutine_threadsafe。
        q.put(
            _WriteTask(task_type, data, commit, None, (event, result)),
            True,
            _QUEUE_PUT_TIMEOUT,
        )

        if not event.wait(_SYNC_WAIT_TIMEOUT):
            raise TimeoutError(
                f"Database write ({task_type}) 等待逾時 ({_SYNC_WAIT_TIMEOUT}s)，"
                "寫入 worker 可能已停止運作。"
            )

        err = result["error"]
        if err is not None:
            if isinstance(err, BaseException):
                raise err
            raise RuntimeError(str(err))
        return result["data"]

    # ------------------------------------------------------------------
    # 結果回填
    # ------------------------------------------------------------------
    @staticmethod
    def _resolve_future(
        future: "asyncio.Future[Any]", value: Any, error: Optional[BaseException]
    ) -> None:
        if future.cancelled() or future.done():
            return
        if error is not None:
            future.set_exception(error)
        else:
            future.set_result(value)

    @classmethod
    def _complete(
        cls, task: _WriteTask, value: Any, error: Optional[BaseException]
    ) -> None:
        if task.future is not None:
            try:
                loop = task.future.get_loop()
            except RuntimeError:
                return
            if loop.is_closed():
                return
            try:
                loop.call_soon_threadsafe(
                    cls._resolve_future, task.future, value, error
                )
            except RuntimeError:
                # loop 已關閉（關機競態），呼叫端本來就不會再讀取結果。
                pass
        elif task.sync_payload is not None:
            event, holder = task.sync_payload
            holder["success"] = error is None
            holder["data"] = value
            holder["error"] = error
            event.set()

    @classmethod
    def _fail_pending(cls, error: BaseException) -> None:
        """worker 意外終止時，把佇列裡尚未處理的任務全部以例外收尾，
        避免呼叫端的 `await future` / `event.wait()` 永久卡死。"""
        q = cls._queue
        if q is None:
            return
        while True:
            try:
                task = q.get_nowait()
            except queue.Empty:
                return
            try:
                if task is not None:
                    cls._complete(task, None, error)
            finally:
                q.task_done()

    # ------------------------------------------------------------------
    # Worker（獨立執行緒）
    # ------------------------------------------------------------------
    @classmethod
    def _worker_main(cls) -> None:
        conn: Optional[sqlite3.Connection] = None
        try:
            conn = connect_db(_WRITER_BUSY_TIMEOUT_MS)
            logger.info("DatabaseWriteQueue worker connection established.")

            q = cls._queue
            assert q is not None

            while True:
                task = q.get()
                try:
                    if task is None:  # sentinel
                        break
                    try:
                        res = cls._run_with_lock_retry(
                            conn, task.task_type, task.data, task.commit
                        )
                        cls._complete(task, res, None)
                    except Exception as e:
                        logger.error(
                            f"Error processing write task {task.task_type}: {e}"
                        )
                        try:
                            conn.rollback()
                        except Exception as rb_err:
                            logger.error(f"Rollback failed: {rb_err}")
                            # 連線本身已不可用時就地重建。舊版遇到這種情況會讓整個
                            # worker 永久退出，佇列從此靜默不再排空。
                            conn = cls._reconnect(conn)
                        cls._complete(task, None, e)
                finally:
                    q.task_done()
        except BaseException as e:  # noqa: BLE001 - worker 不得靜默死亡
            logger.critical(
                f"DatabaseWriteQueue worker loop encountered critical error: {e}",
                exc_info=True,
            )
            cls._running = False
            cls._fail_pending(RuntimeError(f"DatabaseWriteQueue worker 已終止: {e}"))
        finally:
            cls._running = False
            if conn is not None:
                try:
                    conn.close()
                except Exception:
                    pass
                logger.info("DatabaseWriteQueue worker connection closed.")

    @classmethod
    def _reconnect(cls, old: Optional[sqlite3.Connection]) -> sqlite3.Connection:
        if old is not None:
            try:
                old.close()
            except Exception:
                pass
        logger.warning("DatabaseWriteQueue 正在重建寫入連線。")
        return connect_db(_WRITER_BUSY_TIMEOUT_MS)

    @staticmethod
    def _is_lock_error(err: BaseException) -> bool:
        if not isinstance(err, sqlite3.OperationalError):
            return False
        msg = str(err).lower()
        return "locked" in msg or "busy" in msg

    @classmethod
    def _run_with_lock_retry(
        cls, conn: sqlite3.Connection, task_type: str, data: tuple, commit: bool
    ) -> Any:
        """對 SQLITE_BUSY / SQLITE_LOCKED 做 jittered 指數退避重試。

        busy_timeout 只處理「等鎖」，但藍綠部署期間另一個容器可能持有寫入鎖夠久，
        單次等待仍會失敗；退避重試把這種跨程序競爭吸收掉，而不是拋給呼叫端。
        """
        last_err: Optional[BaseException] = None
        for attempt in range(_LOCK_RETRY_ATTEMPTS):
            try:
                return cls._process_task_sync(conn, task_type, data, commit)
            except Exception as e:
                if not cls._is_lock_error(e):
                    raise
                last_err = e
                try:
                    conn.rollback()
                except Exception:
                    pass
                if attempt == _LOCK_RETRY_ATTEMPTS - 1:
                    break
                delay = _LOCK_RETRY_BASE_DELAY * (2**attempt)
                delay += random.uniform(
                    0.0, delay * 0.5
                )  # jitter，避免兩個實例同步重試
                logger.warning(
                    f"寫入遭遇 SQLite 鎖競爭 ({task_type})，{delay:.2f}s 後重試 "
                    f"({attempt + 1}/{_LOCK_RETRY_ATTEMPTS})。"
                )
                time.sleep(delay)

        assert last_err is not None
        raise last_err

    # ------------------------------------------------------------------
    # 實際 SQL 執行
    # ------------------------------------------------------------------
    @classmethod
    def _process_task_sync(
        cls, conn: sqlite3.Connection, task_type: str, data: tuple, commit: bool
    ) -> Any:
        cursor = conn.cursor()

        if task_type == "save_historical_iv":
            symbol, iv, date_str = data
            iv = cls._resolve_iv_with_fallbacks(cursor, symbol, iv)
            if iv is None:
                logger.warning(
                    f"⚠️ [{symbol}] All IV fallbacks failed. Skipping writing to "
                    "historical_iv to prevent NOT NULL constraint error."
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

        elif task_type == "sql_rowcount":
            # 與 "sql" 的差別只在回傳值：忠實回傳 cursor.rowcount。
            # "sql" 的 `lastrowid or rowcount or True` 在 DELETE/UPDATE 命中 0 筆時
            # 會回傳 True，無法與成功區分（database/cache.py 早有註解記載此限制），
            # 因此凡是呼叫端要用「影響筆數」做判斷的，一律走這條。
            query, params = data
            cursor.execute(query, params)
            if commit:
                conn.commit()
            return cursor.rowcount

        elif task_type == "sql_batch":
            # data = (statements,)，statements 為 (query, params, is_many) 序列。
            # 整批共用一個交易、只 commit 一次，供 DELETE+executemany 這類
            # 原本必須自開連線的多語句交易使用。
            (statements,) = data
            rowcounts: list[int] = []
            for query, params, is_many in statements:
                if is_many:
                    cursor.executemany(query, params)
                else:
                    cursor.execute(query, params)
                rowcounts.append(cursor.rowcount)
            if commit:
                conn.commit()
            # 回傳逐語句的影響筆數，讓呼叫端能在「同一個交易內」取得例如
            # DELETE 清除筆數這類資訊，而不必為此另開一次查詢。
            return rowcounts

        else:
            raise ValueError(f"Unknown task type: {task_type}")

    @classmethod
    def _resolve_iv_with_fallbacks(
        cls, cursor: sqlite3.Cursor, symbol: str, iv: Any
    ) -> Optional[float]:
        """IV 自癒：無效值時先查前一交易日收盤 IV，再退到 30 日歷史波動率代理。

        ⚠️ 正常情況下這裡不會觸發——`history_storage.save_historical_iv()` 已在
        **入列前**把無效 IV 解析完畢，避免把網路請求帶進序列化的寫入 worker
        （那會讓全程序所有寫入一起卡住）。此處保留是為了直接以
        `put_task("save_historical_iv", ...)` 呼叫的路徑（含既有測試）仍能自癒。
        """
        import math

        def _invalid(v: Any) -> bool:
            return v is None or (isinstance(v, float) and math.isnan(v))

        if not _invalid(iv):
            return float(iv)

        logger.warning(
            f"[{symbol}] save_historical_iv received invalid IV ({iv}). "
            "Triggering fallback/self-healing."
        )

        # Fallback 1: 前一交易日收盤 IV（純 SQL，可安全留在 worker 執行緒內）
        try:
            cursor.execute(
                "SELECT iv FROM historical_iv WHERE symbol = ? ORDER BY date DESC LIMIT 1",
                (symbol,),
            )
            row = cursor.fetchone()
            if row and not _invalid(row[0]):
                logger.info(
                    f"[{symbol}] Fallback 1: Loaded last closing IV ({row[0]}) from historical_iv."
                )
                return float(row[0])
        except Exception as db_err:
            logger.error(f"[{symbol}] Fallback 1 query failed: {db_err}")

        # Fallback 2: 30 日歷史波動率（HV）代理。需要網路，因此丟回 event loop 執行，
        # 只阻塞本 worker 執行緒，不阻塞 event loop。
        return cls._compute_hv_fallback(symbol)

    @classmethod
    def _compute_hv_fallback(cls, symbol: str) -> Optional[float]:
        try:
            from services.market_data_service import get_history_df
            import pandas as pd
            import numpy as np

            loop = cls._loop
            if loop is not None and loop.is_running():
                fut = asyncio.run_coroutine_threadsafe(
                    get_history_df(symbol, period="1mo"), loop
                )
                df_temp = fut.result(timeout=15)
            else:
                df_temp = asyncio.run(get_history_df(symbol, period="1mo"))

            if not df_temp.empty and len(df_temp) >= 2:
                log_ret = np.log(df_temp["Close"] / df_temp["Close"].shift(1))
                hv = float(log_ret.std() * np.sqrt(252))
                if not pd.isna(hv) and hv > 0:
                    logger.info(
                        f"[{symbol}] Fallback 2: Calculated 30-day HV ({hv}) as fallback."
                    )
                    return hv
        except Exception as hv_err:
            logger.error(f"[{symbol}] Fallback 2 calculation failed: {hv_err}")
        return None

    # ------------------------------------------------------------------
    # 佇列未啟用時的直寫路徑（migration / CLI / 測試）
    # ------------------------------------------------------------------
    @classmethod
    def _execute_direct_write(
        cls, task_type: str, data: tuple, commit: bool = True
    ) -> Any:
        """Direct write fallback when queue is not running.
        Crucial for migrations, tests, and CLI tool stability.
        """
        with _direct_write_lock:
            conn = connect_db()
            try:
                return cls._run_with_lock_retry(conn, task_type, data, commit)
            except Exception:
                try:
                    conn.rollback()
                except Exception:
                    pass
                raise
            finally:
                conn.close()


def execute_write(query: str, params: tuple = (), commit: bool = True) -> Any:
    """Synchronous entry point for all database writes.

    ⚠️ 只能從非 event loop 執行緒呼叫（`asyncio.to_thread`、CLI、測試）；
    從 event loop 執行緒呼叫會拋 RuntimeError（見 `put_task_sync`）。
    """
    return DatabaseWriteQueue.put_task_sync("sql", (query, params), commit)


async def execute_write_async(
    query: str, params: tuple = (), commit: bool = True
) -> Any:
    """Asynchronous entry point for all database writes."""
    return await DatabaseWriteQueue.put_task("sql", (query, params), commit)


def _normalize_statements(
    statements: Sequence[tuple],
) -> list[tuple[str, Any, bool]]:
    normalized: list[tuple[str, Any, bool]] = []
    for stmt in statements:
        if len(stmt) == 2:
            query, params = stmt
            normalized.append((query, params, False))
        elif len(stmt) == 3:
            query, params, is_many = stmt
            normalized.append((query, params, bool(is_many)))
        else:
            raise ValueError(f"Invalid statement tuple: {stmt!r}")
    return normalized


def execute_write_many(statements: Sequence[tuple], commit: bool = True) -> list[int]:
    """同 `execute_write`，但整批語句共用一個交易、只 commit 一次。

    每個 statement 為 `(query, params)` 或 `(query, seq_of_params, True)`
    （後者走 `executemany`）。供 DELETE + executemany 這類原本必須自開連線的
    多語句交易使用。回傳逐語句的 `cursor.rowcount` 清單。
    """
    result = DatabaseWriteQueue.put_task_sync(
        "sql_batch", (_normalize_statements(statements),), commit
    )
    return list(result)


def execute_write_rowcount(query: str, params: tuple = (), commit: bool = True) -> int:
    """同 `execute_write`，但回傳實際影響筆數（`cursor.rowcount`）。"""
    return int(
        DatabaseWriteQueue.put_task_sync("sql_rowcount", (query, params), commit)
    )


async def execute_write_rowcount_async(
    query: str, params: tuple = (), commit: bool = True
) -> int:
    """同 `execute_write_async`，但回傳實際影響筆數（`cursor.rowcount`）。"""
    return int(
        await DatabaseWriteQueue.put_task("sql_rowcount", (query, params), commit)
    )


async def execute_write_many_async(
    statements: Sequence[tuple], commit: bool = True
) -> list[int]:
    """`execute_write_many` 的 async 版本。回傳逐語句的 `cursor.rowcount` 清單。"""
    result = await DatabaseWriteQueue.put_task(
        "sql_batch", (_normalize_statements(statements),), commit
    )
    return list(result)
