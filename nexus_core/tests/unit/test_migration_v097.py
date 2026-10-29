"""單元測試：v097 遷移（wall_anchor / wall_break_events 與 alpha_wall_break 回填）。"""

from __future__ import annotations

import sqlite3

import pytest
from database.core import get_migrations
from database.migrations import v097_add_wall_break_tables as mig


def test_v097_migration_metadata() -> None:
    assert mig.version == 97
    assert "wall_break" in mig.description
    versions = [m["version"] for m in get_migrations()]
    assert versions.count(97) == 1


def test_v097_tables_and_unique_constraint() -> None:
    conn = sqlite3.connect(":memory:")
    try:
        conn.executescript(mig.sql)
        tables = {
            r[0]
            for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
        assert {"wall_anchor", "wall_break_events"} <= tables
        ins = (
            "INSERT INTO wall_break_events (symbol, trade_date, direction, trigger_kind,"
            " bar_time, anchor_trade_date, old_wall, close, verdict, label, gates_json)"
            " VALUES ('NVDA','2026-10-29','BREAKOUT','CLOSE_BEYOND','2026-10-29 10:00',"
            "'2026-10-28',100,101,'CANDIDATE','TRUE_BREAKOUT','{}')"
        )
        conn.execute(ins)
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(ins)
    finally:
        conn.close()


def test_v097_sql_idempotent() -> None:
    conn = sqlite3.connect(":memory:")
    try:
        conn.executescript(mig.sql)
        conn.executescript(mig.sql)
    finally:
        conn.close()


def test_v097_migrate_data_backfill() -> None:
    conn = sqlite3.connect(":memory:")
    try:
        conn.execute(
            "CREATE TABLE user_notification_settings (user_id INTEGER, notification_key TEXT,"
            " enabled INTEGER, PRIMARY KEY (user_id, notification_key))"
        )
        rows = [
            (1, "alpha_price_volume_watch", 0),
            (2, "other_key", 0),
            (3, "alpha_price_volume_watch", 0),
            (3, "alpha_wall_break", 1),
        ]
        conn.executemany("INSERT INTO user_notification_settings VALUES (?,?,?)", rows)
        mig.migrate_data(conn)
        mig.migrate_data(conn)
        got = {
            (r[0], r[1]): r[2]
            for r in conn.execute(
                "SELECT user_id, notification_key, enabled FROM user_notification_settings"
                " WHERE notification_key = 'alpha_wall_break'"
            )
        }
        assert got == {(1, "alpha_wall_break"): 0, (3, "alpha_wall_break"): 1}
    finally:
        conn.close()
