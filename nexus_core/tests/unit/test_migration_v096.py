"""單元測試：v096 遷移（ddp_signals 新增 fiscal_period 與唯一索引）。"""

from __future__ import annotations

import sqlite3

import pytest
from database.core import get_migrations
from database.migrations import v025_add_ddp_signals as mig_v025
from database.migrations import v096_ddp_signal_fiscal_period as mig


def test_v096_migration_metadata() -> None:
    assert mig.version == 96
    assert "fiscal_period" in mig.description
    versions = [m["version"] for m in get_migrations()]
    assert versions.count(96) == 1


def test_v096_unique_index_and_null_coexist() -> None:
    conn = sqlite3.connect(":memory:")
    try:
        conn.executescript(mig_v025.sql)
        conn.execute("INSERT INTO ddp_signals (symbol) VALUES ('OLD')")
        conn.execute("INSERT INTO ddp_signals (symbol) VALUES ('OLD')")
        conn.executescript(mig.sql)
        cols = [r[1] for r in conn.execute("PRAGMA table_info(ddp_signals)")]
        assert "fiscal_period" in cols
        # 舊資料 NULL 並存
        conn.execute("INSERT INTO ddp_signals (symbol) VALUES ('OLD')")
        conn.execute(
            "INSERT INTO ddp_signals (symbol, fiscal_period) VALUES ('NVDA', '2026-07-26')"
        )
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO ddp_signals (symbol, fiscal_period) VALUES ('NVDA', '2026-07-26')"
            )
        conn.execute(
            "INSERT INTO ddp_signals (symbol, fiscal_period) VALUES ('NVDA', '2026-10-26')"
        )
    finally:
        conn.close()
