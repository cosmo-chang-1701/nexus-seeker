import os
import tempfile
from datetime import datetime
from typing import Any, Generator

import pytest

import database
import scheduler


@pytest.fixture(autouse=True)
def _isolated_db(monkeypatch: pytest.MonkeyPatch) -> Generator[None, None, None]:
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    monkeypatch.setattr(database, "DB_PATH", path)
    database.init_db()
    # _poll_cursor 是模組層級游標，跨測試共用同一個 process，測試間必須重置，
    # 避免前一個測試留下的游標位置讓本測試的批次輪替變得不可預期。
    monkeypatch.setattr(scheduler, "_poll_cursor", 0)
    yield
    try:
        os.remove(path)
    except OSError:
        pass


class _FakeBrowser:
    async def close(self) -> None:
        pass


class _FakeChromium:
    async def launch(self, **kwargs: object) -> "_FakeBrowser":
        return _FakeBrowser()


class _FakePlaywright:
    chromium = _FakeChromium()


class _FakeAsyncPlaywrightCtx:
    async def __aenter__(self) -> "_FakePlaywright":
        return _FakePlaywright()

    async def __aexit__(self, *args: object) -> None:
        pass


@pytest.mark.asyncio
async def test_poll_once_writes_snapshots_for_tracked_symbols(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database.upsert_tracked_symbols(["AAPL", "TSLA"])

    async def _fake_gex_core(symbol: str, browser: object) -> dict[str, Any]:
        return {
            "spot": 100.0,
            "net_gex": 1.0,
            "call_wall": 110.0,
            "put_wall": 90.0,
            "gex_profile": {"100.0": 1.0},
        }

    async def _fake_expiries(symbol: str) -> list[str]:
        return ["2026-09-18"]

    async def _fake_chain(symbol: str, expiry: str) -> dict[str, Any]:
        return {"calls": [{"strike": 100.0}], "puts": [{"strike": 90.0}]}

    monkeypatch.setattr(scheduler, "scrape_symbol_gex_core", _fake_gex_core)
    monkeypatch.setattr(scheduler, "fetch_option_expiries", _fake_expiries)
    monkeypatch.setattr(scheduler, "fetch_option_chain_dict", _fake_chain)
    monkeypatch.setattr(
        scheduler, "async_playwright", lambda: _FakeAsyncPlaywrightCtx()
    )

    # 分批輪詢：2 個標的、POLL_ROTATION_CYCLES=6 時每輪批次大小為 1，
    # 需呼叫兩次 poll_once() 讓 round-robin 游標輪過所有標的。
    await scheduler.poll_once()
    await scheduler.poll_once()

    for sym in ["AAPL", "TSLA"]:
        gex_row = database.get_gex_snapshot(sym)
        assert gex_row is not None
        assert gex_row["call_wall"] == 110.0

        chain_row = database.get_option_chain_snapshot(sym)
        assert chain_row is not None
        assert chain_row["expiry"] == "2026-09-18"
        assert chain_row["calls"] == [{"strike": 100.0}]


@pytest.mark.asyncio
async def test_poll_once_is_noop_when_no_tracked_symbols(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    called = {"n": 0}

    def _fake_launch_tracker(*args: object, **kwargs: object) -> None:
        called["n"] += 1

    monkeypatch.setattr(scheduler, "async_playwright", lambda: _fake_launch_tracker())

    # 沒有任何 tracked symbol 時應直接 return，不觸發 Playwright。
    await scheduler.poll_once()
    assert called["n"] == 0


@pytest.mark.asyncio
async def test_poll_once_continues_when_one_symbol_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database.upsert_tracked_symbols(["BAD", "GOOD"])

    async def _fake_gex_core(symbol: str, browser: object) -> dict[str, Any]:
        if symbol == "BAD":
            raise RuntimeError("scrape failed")
        return {
            "spot": 1.0,
            "net_gex": 1.0,
            "call_wall": 1.0,
            "put_wall": 1.0,
            "gex_profile": {},
        }

    async def _fake_expiries(symbol: str) -> list[str]:
        return []

    async def _fake_chain(symbol: str, expiry: str) -> dict[str, Any]:
        return {"calls": [], "puts": []}

    monkeypatch.setattr(scheduler, "scrape_symbol_gex_core", _fake_gex_core)
    monkeypatch.setattr(scheduler, "fetch_option_expiries", _fake_expiries)
    monkeypatch.setattr(scheduler, "fetch_option_chain_dict", _fake_chain)
    monkeypatch.setattr(
        scheduler, "async_playwright", lambda: _FakeAsyncPlaywrightCtx()
    )

    # 分批輪詢：2 個標的、POLL_ROTATION_CYCLES=6 時每輪批次大小為 1，
    # 需呼叫兩次 poll_once() 讓 round-robin 游標輪過所有標的。
    await scheduler.poll_once()
    await scheduler.poll_once()

    assert database.get_gex_snapshot("BAD") is None
    good_row = database.get_gex_snapshot("GOOD")
    assert good_row is not None
    assert good_row["call_wall"] == 1.0


def test_prune_runs_after_poll(monkeypatch: pytest.MonkeyPatch) -> None:
    database.upsert_tracked_symbols(["OLD"])
    conn = database._get_connection()
    try:
        conn.execute(
            "UPDATE tracked_symbols SET last_synced_at = datetime('now', '-72 hours') WHERE symbol = 'OLD'"
        )
        conn.commit()
    finally:
        conn.close()

    removed = database.prune_stale_symbols(48)
    assert removed == 1
    assert database.get_tracked_symbols() == []


def test_is_us_market_hours_weekday_during_session() -> None:
    from zoneinfo import ZoneInfo

    class _FixedDateTime(datetime):
        @classmethod
        def now(cls, tz: Any = None) -> "_FixedDateTime":
            return cls(2026, 8, 19, 10, 30, tzinfo=ZoneInfo("America/New_York"))

    import scheduler as scheduler_module

    original = scheduler_module.datetime
    scheduler_module.datetime = _FixedDateTime  # type: ignore[misc]
    try:
        assert scheduler_module._is_us_market_hours() is True
    finally:
        scheduler_module.datetime = original  # type: ignore[misc]


def test_is_us_market_hours_weekend_is_false() -> None:
    from zoneinfo import ZoneInfo

    class _FixedDateTime(datetime):
        @classmethod
        def now(cls, tz: Any = None) -> "_FixedDateTime":
            # 2026-08-22 is a Saturday
            return cls(2026, 8, 22, 10, 30, tzinfo=ZoneInfo("America/New_York"))

    import scheduler as scheduler_module

    original = scheduler_module.datetime
    scheduler_module.datetime = _FixedDateTime  # type: ignore[misc]
    try:
        assert scheduler_module._is_us_market_hours() is False
    finally:
        scheduler_module.datetime = original  # type: ignore[misc]


def test_start_and_stop_are_idempotent() -> None:
    import asyncio

    async def _run() -> None:
        scheduler.start()
        assert scheduler._task is not None
        scheduler.start()  # second call should be a no-op, not raise
        scheduler.stop()
        assert scheduler._task is None
        scheduler.stop()  # stopping twice should not raise

    asyncio.run(_run())
