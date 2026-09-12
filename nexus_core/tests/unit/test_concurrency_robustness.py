from typing import Any
import asyncio
import sqlite3
import threading
import pytest
from unittest.mock import AsyncMock, patch
import pandas as pd

from services.single_flight import SingleFlightManager
from database.connection import (
    DatabaseWriteQueue,
    execute_write_async,
    get_read_connection,
)


@pytest.mark.asyncio
async def test_single_flight_coalescing() -> Any:
    """Test that SingleFlightManager coalesces multiple concurrent requests for the same key."""
    call_count = 0

    async def mock_analysis_task(symbol: str) -> Any:
        nonlocal call_count
        call_count += 1
        await asyncio.sleep(0.1)  # Simulate some processing time
        return f"result_{symbol}_{call_count}"

    # Query the same key 'analyze_AAPL' 5 times concurrently
    tasks = [
        SingleFlightManager.run("analyze_AAPL", mock_analysis_task, "AAPL")
        for _ in range(5)
    ]

    results = await asyncio.gather(*tasks)

    # All tasks must return the result of the first task execution
    for res in results:
        assert res == "result_AAPL_1"

    # The actual task should only be called once
    assert call_count == 1

    # After it finishes, a new query should trigger a new task
    new_res = await SingleFlightManager.run("analyze_AAPL", mock_analysis_task, "AAPL")
    assert new_res == "result_AAPL_2"
    assert call_count == 2


@pytest.mark.asyncio
async def test_database_write_queue_integration(db_conn: Any):  # type: ignore
    """Test that DatabaseWriteQueue processes queries sequentially and correctly."""
    loop = asyncio.get_running_loop()

    # Initialize the queue
    DatabaseWriteQueue.initialize(loop)
    assert DatabaseWriteQueue.is_active() is True

    try:
        # Perform concurrent writes using the write queue via execute_write_async
        tasks = [
            execute_write_async(
                "INSERT INTO kv_cache (key, value, updated_at) VALUES (?, ?, CURRENT_TIMESTAMP)",
                (f"key_{i}", f"val_{i}"),
            )
            for i in range(10)
        ]

        # Wait for all writes to finish
        results = await asyncio.gather(*tasks)
        for res in results:
            assert res is True or isinstance(res, int)

        # Query the database to verify all values were inserted successfully
        conn = get_read_connection()
        cursor = conn.cursor()
        cursor.execute(
            "SELECT key, value FROM kv_cache WHERE key LIKE 'key_%' ORDER BY key ASC"
        )
        rows = cursor.fetchall()
        conn.close()

        assert len(rows) == 10
        for i, (key, value) in enumerate(rows):
            assert key == f"key_{i}"
            assert "val_" in value

    finally:
        await DatabaseWriteQueue.stop_worker()
        assert DatabaseWriteQueue.is_active() is False


@pytest.mark.asyncio
async def test_historical_iv_self_healing_fallback(db_conn: Any):  # type: ignore
    """Test that save_historical_iv performs fallback self-healing when IV is None."""
    loop = asyncio.get_running_loop()
    DatabaseWriteQueue.initialize(loop)

    symbol = "COOLDOWN_TEST"
    date_str = "2026-06-16"

    try:
        # Setup: insert some previous day closing IV
        await execute_write_async(
            "INSERT INTO historical_iv (symbol, iv, date) VALUES (?, ?, ?)",
            (symbol, 0.45, "2026-06-15"),
        )

        # 1. Test Fallback 1: Yesterday's Closing IV when iv is None
        # Submit a save task where iv is None
        fut = await DatabaseWriteQueue.put_task(
            "save_historical_iv", (symbol, None, date_str)
        )
        assert fut is True

        # Verify it saved 0.45 (yesterday's closing IV)
        conn = get_read_connection()
        cursor = conn.cursor()
        cursor.execute(
            "SELECT iv FROM historical_iv WHERE symbol = ? AND date = ?",
            (symbol, date_str),
        )
        row = cursor.fetchone()
        assert row is not None
        assert row[0] == pytest.approx(0.45)

        # 2. Test Fallback 2: 30-day Historical Volatility when yesterday's IV is not in DB
        symbol2 = "HV_TEST"
        # Mock market_data_service.get_history_df to return stock prices
        dates = pd.date_range(end="2026-06-16", periods=30, freq="D")
        # Constant returns of alternating +/- 1% to get standard deviation
        prices = [100.0]
        for _ in range(29):
            prices.append(prices[-1] * (1.01 if len(prices) % 2 == 0 else 0.99))
        df_hist = pd.DataFrame({"Close": prices}, index=dates)

        with patch(
            "services.market_data_service.get_history_df", new_callable=AsyncMock
        ) as mock_hist:
            mock_hist.return_value = df_hist

            # Save historical IV with None for symbol2 (which has no database history)
            fut2 = await DatabaseWriteQueue.put_task(
                "save_historical_iv", (symbol2, None, date_str)
            )
            assert fut2 is True

            # Verify it saved a non-zero value computed from the mock stock prices
            cursor.execute(
                "SELECT iv FROM historical_iv WHERE symbol = ? AND date = ?",
                (symbol2, date_str),
            )
            row2 = cursor.fetchone()
            assert row2 is not None
            assert row2[0] > 0.0

        # 3. Test Fallback 3: Skip writing when all fallbacks fail
        symbol3 = "FAIL_TEST"
        with patch(
            "services.market_data_service.get_history_df", new_callable=AsyncMock
        ) as mock_hist:
            mock_hist.return_value = (
                pd.DataFrame()
            )  # Empty dataframe, forces HV proxy to fail

            fut3 = await DatabaseWriteQueue.put_task(
                "save_historical_iv", (symbol3, None, date_str)
            )
            # Should skip writing and return False
            assert fut3 is False

            cursor.execute(
                "SELECT 1 FROM historical_iv WHERE symbol = ? AND date = ?",
                (symbol3, date_str),
            )
            row3 = cursor.fetchone()
            assert (
                row3 is None
            )  # Verify no record was inserted, preventing NOT NULL constraint error

        conn.close()

    finally:
        await DatabaseWriteQueue.stop_worker()


@pytest.mark.asyncio
async def test_put_task_sync_backpressure_no_deadlock(db_conn: Any) -> None:
    """驗證加上 maxsize 之後，put_task_sync 在佇列滿載時是正確地非阻塞式等待
    （最終在有空間時成功完成），而不是永久卡死。"""
    loop = asyncio.get_running_loop()
    hold_event = threading.Event()
    # 透過 __dict__ 取原始 classmethod 描述器再取 __func__，避開直接對已綁定的
    # classmethod 存取 __func__ 時，mypy 對 bound method 型別沒有該屬性的靜態檢查錯誤。
    original_process_task = DatabaseWriteQueue.__dict__["_process_task_sync"].__func__

    def slow_process_task(
        cls: Any, conn: Any, task_type: str, data: tuple, commit: bool
    ) -> Any:
        if task_type == "hold":
            # 讓 worker 卡在這裡，模擬處理緩慢導致佇列積壓的情境。
            # 注意這是 threading.Event 而非 asyncio.Event——worker 已經是獨立
            # 執行緒，不再跑在 event loop 上。
            hold_event.wait(10.0)
            return True
        return original_process_task(cls, conn, task_type, data, commit)

    with patch.object(
        DatabaseWriteQueue, "_process_task_sync", classmethod(slow_process_task)
    ):
        # maxsize=1：只要 worker 卡在第一筆任務，第二筆就會把佇列填滿。
        DatabaseWriteQueue.initialize(loop, maxsize=1)
        try:
            # Task 1：讓 worker 從 queue.get() 拿走後卡住。
            fut_hold = asyncio.ensure_future(DatabaseWriteQueue.put_task("hold", ()))
            await asyncio.sleep(0.1)

            # Task 2：worker 正忙，這筆會佔滿 maxsize=1 的佇列。
            fut_2 = asyncio.ensure_future(
                DatabaseWriteQueue.put_task("sql", ("SELECT 1", ()))
            )
            await asyncio.sleep(0.1)
            assert DatabaseWriteQueue._queue is not None
            assert DatabaseWriteQueue._queue.full()

            # Task 3：改由 worker thread（run_in_executor）呼叫 put_task_sync，
            # 此時佇列已滿，應該正確地等待而不是死結或立即失敗。
            fut_sync = loop.run_in_executor(
                None,
                DatabaseWriteQueue.put_task_sync,
                "sql",
                ("SELECT 1", ()),
                False,
            )
            await asyncio.sleep(0.2)
            assert not fut_sync.done(), (
                "佇列已滿時 put_task_sync 應該正確等待背壓釋放，"
                "而不是立刻完成或拋錯（代表沒有真的觸發滿載路徑）"
            )

            # 釋放 worker，讓積壓的任務依序被處理完，確認整條鏈最終都能完成。
            hold_event.set()
            await asyncio.wait_for(fut_hold, timeout=5.0)
            await asyncio.wait_for(fut_2, timeout=5.0)
            result = await asyncio.wait_for(fut_sync, timeout=5.0)
            assert result is not None
        finally:
            hold_event.set()
            await DatabaseWriteQueue.stop_worker()


@pytest.mark.asyncio
async def test_write_worker_runs_off_the_event_loop(db_conn: Any) -> None:
    """寫入必須在專屬背景執行緒上執行，絕不能在 event loop 執行緒上。

    這是 `heartbeat blocked for more than 10 seconds` 的根因回歸測試：早期版本用
    `loop.create_task(_worker_loop())`，`cursor.execute()` 的 busy handler 會直接
    睡在 Discord gateway 的執行緒上。
    """
    loop = asyncio.get_running_loop()
    loop_thread = threading.current_thread()
    seen: dict[str, Any] = {}

    original_process_task = DatabaseWriteQueue.__dict__["_process_task_sync"].__func__

    def recording_process_task(
        cls: Any, conn: Any, task_type: str, data: tuple, commit: bool
    ) -> Any:
        seen["thread"] = threading.current_thread()
        return original_process_task(cls, conn, task_type, data, commit)

    with patch.object(
        DatabaseWriteQueue, "_process_task_sync", classmethod(recording_process_task)
    ):
        DatabaseWriteQueue.initialize(loop)
        try:
            await execute_write_async(
                "INSERT INTO kv_cache (key, value, updated_at) VALUES (?, ?, CURRENT_TIMESTAMP)",
                ("off_loop_probe", "1"),
            )
        finally:
            await DatabaseWriteQueue.stop_worker()

    assert seen.get("thread") is not None
    assert (
        seen["thread"] is not loop_thread
    ), "寫入不得在 event loop 執行緒上執行，否則 SQLite busy handler 會阻塞 Discord 心跳"
    assert seen["thread"].name == "nexus-db-writer"


@pytest.mark.asyncio
async def test_write_storm_does_not_stall_the_event_loop(db_conn: Any) -> None:
    """灌入大量寫入時，event loop 仍須保持可回應。

    以一個 20ms 週期的探針量測 loop 延遲；修復前每筆寫入都在 loop 上同步
    commit，延遲會隨寫入量線性累積。
    """
    loop = asyncio.get_running_loop()
    DatabaseWriteQueue.initialize(loop)
    max_lag = 0.0
    stop = asyncio.Event()

    async def probe() -> None:
        nonlocal max_lag
        while not stop.is_set():
            t0 = loop.time()
            await asyncio.sleep(0.02)
            max_lag = max(max_lag, loop.time() - t0 - 0.02)

    probe_task = asyncio.ensure_future(probe())
    try:
        await asyncio.gather(
            *[
                execute_write_async(
                    "INSERT INTO kv_cache (key, value, updated_at) VALUES (?, ?, CURRENT_TIMESTAMP)",
                    (f"storm_{i}", "v"),
                )
                for i in range(300)
            ]
        )
    finally:
        stop.set()
        await probe_task
        await DatabaseWriteQueue.stop_worker()

    assert max_lag < 1.0, f"event loop 於寫入風暴期間停滯 {max_lag:.2f}s"


@pytest.mark.asyncio
async def test_write_queue_retries_on_database_locked(db_conn: Any) -> None:
    """SQLITE_BUSY 應被退避重試吸收，而不是冒泡成 `database is locked` 給呼叫端。

    藍綠部署期間兩個容器並存掛載同一個 DB volume，單一寫入佇列在該窗口內
    必然失效，因此跨程序鎖競爭必須靠重試而非單純的 busy_timeout。
    """
    loop = asyncio.get_running_loop()
    attempts = {"n": 0}
    original_process_task = DatabaseWriteQueue.__dict__["_process_task_sync"].__func__

    def flaky_process_task(
        cls: Any, conn: Any, task_type: str, data: tuple, commit: bool
    ) -> Any:
        if task_type == "sql":
            attempts["n"] += 1
            if attempts["n"] <= 2:
                raise sqlite3.OperationalError("database is locked")
        return original_process_task(cls, conn, task_type, data, commit)

    with patch.object(
        DatabaseWriteQueue, "_process_task_sync", classmethod(flaky_process_task)
    ):
        DatabaseWriteQueue.initialize(loop)
        try:
            res = await execute_write_async(
                "INSERT INTO kv_cache (key, value, updated_at) VALUES (?, ?, CURRENT_TIMESTAMP)",
                ("retry_probe", "1"),
            )
        finally:
            await DatabaseWriteQueue.stop_worker()

    assert attempts["n"] == 3, "應該重試到第 3 次才成功"
    assert res is True or isinstance(res, int)

    conn = get_read_connection()
    try:
        cur = conn.cursor()
        cur.execute("SELECT value FROM kv_cache WHERE key = ?", ("retry_probe",))
        assert cur.fetchone() is not None
    finally:
        conn.close()


@pytest.mark.asyncio
async def test_stop_worker_drains_queued_writes(db_conn: Any) -> None:
    """stop_worker() 必須先把佇列中尚未寫入的任務排空才退出，而不是直接取消。"""
    loop = asyncio.get_running_loop()
    DatabaseWriteQueue.initialize(loop)

    futures = [
        asyncio.ensure_future(
            execute_write_async(
                "INSERT INTO kv_cache (key, value, updated_at) VALUES (?, ?, CURRENT_TIMESTAMP)",
                (f"drain_{i}", "v"),
            )
        )
        for i in range(50)
    ]
    # 先讓這些 task 真的把任務放進佇列（ensure_future 只是排程，還沒執行過
    # put_task 的入列動作），才能驗證 stop_worker() 是否把佇列排空。
    await asyncio.sleep(0.05)
    await DatabaseWriteQueue.stop_worker()
    await asyncio.gather(*futures)

    conn = get_read_connection()
    try:
        cur = conn.cursor()
        cur.execute("SELECT COUNT(*) FROM kv_cache WHERE key LIKE 'drain_%'")
        assert cur.fetchone()[0] == 50
    finally:
        conn.close()


@pytest.mark.asyncio
async def test_execute_write_many_async_is_one_transaction(db_conn: Any) -> None:
    """批次寫入入口：多語句共用一個交易，支援 executemany。"""
    from database.connection import execute_write_many_async

    loop = asyncio.get_running_loop()
    DatabaseWriteQueue.initialize(loop)
    try:
        await execute_write_many_async(
            [
                ("DELETE FROM kv_cache WHERE key LIKE ?", ("batch_%",)),
                (
                    "INSERT INTO kv_cache (key, value, updated_at) VALUES (?, ?, CURRENT_TIMESTAMP)",
                    [(f"batch_{i}", "v") for i in range(5)],
                    True,
                ),
            ]
        )
    finally:
        await DatabaseWriteQueue.stop_worker()

    conn = get_read_connection()
    try:
        cur = conn.cursor()
        cur.execute("SELECT COUNT(*) FROM kv_cache WHERE key LIKE 'batch_%'")
        assert cur.fetchone()[0] == 5
    finally:
        conn.close()


@pytest.mark.asyncio
async def test_cache_writers_succeed_with_write_queue_active(db_conn: Any):  # type: ignore
    """market_cache / squeeze_cache 的寫入函式必須能在「寫入佇列已啟用」的狀態下
    （即 production 的真實狀態）從 event loop 成功寫入。

    這則測試存在的理由：其餘所有 market_cache / squeeze_cache 測試都是在佇列未啟用
    （`is_active()` 為 False）下跑的，`put_task_sync()` 會走 `_execute_direct_write()`
    直寫捷徑，因此 `threading.current_thread() is cls._loop_thread` 守衛永遠不會觸發。
    這讓「在 event loop 上同步寫入」的缺陷在測試中完全隱形，只在 production 拋
    `Sync database write (sql) called from main event loop thread`（且多數呼叫點的
    `except` 連 log 都沒有，屬於全靜默失效）。
    """
    from database.market_cache import (
        save_market_cache,
        mark_market_cache_stale,
        save_fundamental_scan_state,
        get_market_cache,
        get_fundamental_scan_state,
    )
    from database.squeeze_cache import save_squeeze_cache, get_squeeze_cache

    loop = asyncio.get_running_loop()
    DatabaseWriteQueue.initialize(loop)
    assert DatabaseWriteQueue.is_active() is True

    try:
        assert (
            await save_market_cache(
                "QUEUE_MC",
                100.0,
                95.0,
                105.0,
                reference_spot_price=100.0,
                call_wall=150.0,
            )
            is True
        )
        row = get_market_cache("QUEUE_MC")
        assert row is not None and row.get("call_wall") == 150.0
        assert row.get("is_stale") == 0

        assert await mark_market_cache_stale("QUEUE_MC") is True
        row = get_market_cache("QUEUE_MC")
        assert row is not None and row.get("is_stale") == 1

        # 注意：save_fundamental_cache / get_fundamental_cache 刻意未納入本測試。
        # fundamental_cache 資料表實際上並不存在——v057_fundamental_cache.py 用的是
        # upgrade(cursor) 介面，而 database/core.py::get_migrations() 只收錄同時具備
        # version / description / sql 三個模組層級屬性的遷移模組，因此該遷移被無聲跳過
        # （v054_add_cro_risk_settings.py 的 run(conn) 介面同樣被跳過）。那是與本次
        # event loop 同步寫入缺陷互相獨立的另一個問題，需以新遷移補建資料表後才能納入。
        assert (
            await save_fundamental_scan_state("QUEUE_MC", "0001-24-000001", "10-Q")
            is True
        )
        st = get_fundamental_scan_state("QUEUE_MC")
        assert st is not None
        assert st.get("last_accession_number") == "0001-24-000001"

        assert await save_squeeze_cache("QUEUE_SQZ", True, 15.5, "🟢") is True
        sq = get_squeeze_cache("QUEUE_SQZ")
        assert sq is not None and sq.get("momentum") == 15.5
    finally:
        await DatabaseWriteQueue.stop_worker()
        assert DatabaseWriteQueue.is_active() is False


@pytest.mark.asyncio
async def test_run_maintenance_executes_on_the_writer(db_conn: Any) -> None:
    """WAL checkpoint 與 PRAGMA optimize 必須經由寫入佇列在 writer 連線上執行。

    checkpoint 需要寫入權限，且必須與其他寫入序列化，因此不能自開連線。
    專案先前完全沒有任何 wal_checkpoint / PRAGMA optimize：WAL 只在
    auto-checkpoint 成功時才回收，而 kv_cache 每 15 分鐘大量改寫，
    查詢規劃器卻從未取得統計資訊。
    """
    from database.connection import run_maintenance

    loop = asyncio.get_running_loop()
    DatabaseWriteQueue.initialize(loop)
    try:
        notes = await asyncio.to_thread(run_maintenance)
    finally:
        await DatabaseWriteQueue.stop_worker()

    assert "wal_checkpoint" in notes
    assert "optimize" in notes


def test_run_maintenance_refuses_to_run_on_the_event_loop() -> None:
    """維護作業是同步阻塞呼叫，從 event loop 執行緒呼叫必須被擋下。"""
    from database.connection import run_maintenance

    async def _main() -> None:
        loop = asyncio.get_running_loop()
        DatabaseWriteQueue.initialize(loop)
        try:
            with pytest.raises(RuntimeError, match="event loop thread"):
                run_maintenance()
        finally:
            await DatabaseWriteQueue.stop_worker()

    asyncio.run(_main())
