"""單元測試：database/wall_break.py（牆錨點與突破事件存取層）。"""

from __future__ import annotations

from typing import Any

import pytest
from database.connection import execute_write_async, get_read_connection
from database.wall_break import (
    WALL_BREAK_EVENT_COLUMNS,
    get_latest_wall_anchor_before,
    get_pending_t1_events,
    get_wall_break_event,
    insert_wall_break_event,
    purge_stale_wall_break,
    save_wall_break_t1,
    upsert_wall_anchor,
)


@pytest.fixture(autouse=True)
async def _clean_tables(db_conn: Any) -> Any:
    await execute_write_async("DELETE FROM wall_anchor")
    await execute_write_async("DELETE FROM wall_break_events")
    yield
    await execute_write_async("DELETE FROM wall_anchor")
    await execute_write_async("DELETE FROM wall_break_events")


def _event(**over: Any) -> dict[str, Any]:
    ev: dict[str, Any] = {
        "symbol": "nvda",
        "trade_date": "2026-10-29",
        "direction": "BREAKOUT",
        "trigger_kind": "CLOSE_BEYOND",
        "bar_time": "2026-10-29 10:00",
        "anchor_trade_date": "2026-10-28",
        "old_wall": 100.0,
        "close": 101.5,
        "verdict": "CANDIDATE",
        "label": "TRUE_BREAKOUT",
        "gates_json": "{}",
    }
    ev.update(over)
    return ev


def test_columns_exclude_system_fields() -> None:
    assert "id" not in WALL_BREAK_EVENT_COLUMNS
    assert "created_at" not in WALL_BREAK_EVENT_COLUMNS
    assert not any(c.startswith("t1_") for c in WALL_BREAK_EVENT_COLUMNS)


async def test_upsert_then_overwrite() -> None:
    assert await upsert_wall_anchor("nvda", "2026-10-28", 110.0, 90.0, "2026-10-30")
    assert await upsert_wall_anchor("NVDA", "2026-10-28", 112.0, 91.0, "2026-10-30")
    row = get_latest_wall_anchor_before("nvda", "2026-10-29")
    assert row is not None
    assert row["symbol"] == "NVDA"
    assert row["call_wall"] == 112.0
    assert row["put_wall"] == 91.0
    assert row["gex_expiry"] == "2026-10-30"


async def test_latest_before_skips_same_day() -> None:
    await upsert_wall_anchor("NVDA", "2026-10-26", 100.0, 80.0, None)
    await upsert_wall_anchor("NVDA", "2026-10-27", 101.0, 81.0, None)
    await upsert_wall_anchor("NVDA", "2026-10-29", 999.0, 1.0, None)
    row = get_latest_wall_anchor_before("NVDA", "2026-10-29")
    assert row is not None and row["trade_date"] == "2026-10-27"
    assert get_latest_wall_anchor_before("NVDA", "2026-10-26") is None
    assert get_latest_wall_anchor_before("AMD", "2026-10-29") is None


async def test_non_positive_walls_stored_null() -> None:
    await upsert_wall_anchor("NVDA", "2026-10-28", 0.0, -5.0, None)
    row = get_latest_wall_anchor_before("NVDA", "2026-10-29")
    assert row is not None
    assert row["call_wall"] is None and row["put_wall"] is None


async def test_insert_or_ignore() -> None:
    assert await insert_wall_break_event(_event()) is True
    assert await insert_wall_break_event(_event(close=105.0)) is False
    row = get_wall_break_event("NVDA", "2026-10-29", "BREAKOUT", "CLOSE_BEYOND")
    assert row is not None
    assert row["close"] == 101.5 and row["symbol"] == "NVDA"
    assert row["old_wall_oi"] is None
    assert (
        get_wall_break_event("NVDA", "2026-10-29", "BREAKDOWN", "CLOSE_BEYOND") is None
    )


async def test_pending_filters() -> None:
    await insert_wall_break_event(_event(trade_date="2026-10-27"))
    await insert_wall_break_event(
        _event(trade_date="2026-10-27", trigger_kind="WICK_REJECT")
    )
    await insert_wall_break_event(_event(trade_date="2026-10-29"))
    await insert_wall_break_event(_event(trade_date="2026-10-28"))
    settled = get_wall_break_event("NVDA", "2026-10-28", "BREAKOUT", "CLOSE_BEYOND")
    assert settled is not None
    assert await save_wall_break_t1(settled["id"], "2026-10-29", "FAKE", "X", "{}")
    pending = get_pending_t1_events("nvda", "2026-10-29")
    assert [(p["trade_date"], p["trigger_kind"]) for p in pending] == [
        ("2026-10-27", "CLOSE_BEYOND")
    ]
    assert get_pending_t1_events("AMD", "2026-10-29") == []


async def test_save_t1_only_once() -> None:
    await insert_wall_break_event(_event())
    row = get_wall_break_event("NVDA", "2026-10-29", "BREAKOUT", "CLOSE_BEYOND")
    assert row is not None
    assert await save_wall_break_t1(row["id"], "2026-10-30", "CONFIRMED", "A", "{}")
    assert not await save_wall_break_t1(row["id"], "2026-10-30", "FAKE", "B", "{}")
    row = get_wall_break_event("NVDA", "2026-10-29", "BREAKOUT", "CLOSE_BEYOND")
    assert row is not None
    assert row["t1_verdict"] == "CONFIRMED" and row["t1_checked_at"] is not None


async def test_purge_stale() -> None:
    await upsert_wall_anchor("NVDA", "2000-01-01", 1.0, 1.0, None)
    await upsert_wall_anchor("NVDA", "2999-01-01", 1.0, 1.0, None)
    await insert_wall_break_event(_event(trade_date="2026-10-01"))
    await insert_wall_break_event(_event(trade_date="2026-10-02"))
    await execute_write_async(
        "UPDATE wall_break_events SET created_at = datetime('now', '-200 days')"
        " WHERE trade_date = '2026-10-01'"
    )
    assert await purge_stale_wall_break() == 2
    conn = get_read_connection()
    try:
        a = conn.execute("SELECT COUNT(*) FROM wall_anchor").fetchone()[0]
        e = conn.execute("SELECT COUNT(*) FROM wall_break_events").fetchone()[0]
    finally:
        conn.close()
    assert (a, e) == (1, 1)
