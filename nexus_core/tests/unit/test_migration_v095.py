"""單元測試：v095 遷移（fair_value_log 新增 comps_detail_json）。"""

from __future__ import annotations

import sqlite3
from typing import Any

import pytest
from database.core import get_migrations
from database.fundamental_pipeline import get_fair_value, save_fair_value
from database.migrations import v092_add_earnings_surprise as mig_v092
from database.migrations import v094_add_valuation_and_watch as mig_v094
from database.migrations import v095_add_fair_value_comps_detail as mig
from market_analysis.fundamental_pipeline.models import FairValueRecord


def test_v095_migration_metadata() -> None:
    assert mig.version == 95
    assert "comps_detail_json" in mig.description
    assert "ALTER TABLE fair_value_log ADD COLUMN comps_detail_json" in mig.sql
    versions = [m["version"] for m in get_migrations()]
    assert 95 in versions
    assert versions.count(95) == 1


def test_v095_adds_nullable_column_on_top_of_v094() -> None:
    conn = sqlite3.connect(":memory:")
    try:
        conn.executescript(mig_v092.sql)
        conn.executescript(mig_v094.sql)
        # 套用前：舊資料列存在、沒有新欄位
        conn.execute(
            """
            INSERT INTO fair_value_log (
                symbol, trading_date, discount_rate, equity_risk_premium, flags_json
            ) VALUES ('OLD', '2026-10-05', 0.08, 0.045, '[]')
            """
        )
        cols_before = [
            r[1] for r in conn.execute("PRAGMA table_info(fair_value_log)").fetchall()
        ]
        assert "comps_detail_json" not in cols_before

        conn.executescript(mig.sql)
        info = {
            r[1]: (r[2], int(r[3]))
            for r in conn.execute("PRAGMA table_info(fair_value_log)").fetchall()
        }
        assert info["comps_detail_json"] == ("TEXT", 0)  # 型別 TEXT、可為 NULL
        row = conn.execute(
            "SELECT comps_detail_json FROM fair_value_log WHERE symbol = 'OLD'"
        ).fetchone()
        assert row == (None,)
    finally:
        conn.close()


@pytest.mark.asyncio
async def test_comps_detail_json_roundtrip_and_upsert(db_conn: Any) -> None:
    """寫入、讀回，並驗證 ON CONFLICT 更新會覆寫新欄位；舊式記錄（None）讀回為 None。"""
    base = dict(
        symbol="MU",
        trading_date="2026-10-13",
        dcf_value=1119.99,
        comps_value=5353.16,
        fair_value=3236.57,
        margin_of_safety=0.7055,
        discount_rate=0.1325,
        equity_risk_premium=0.04,
        flags_json="[]",
        method="BLENDED",
        spot_price=953.19,
    )
    await save_fair_value(FairValueRecord(**base))  # type: ignore[arg-type]
    first = get_fair_value("MU", "2026-10-13")
    assert first is not None and first.comps_detail_json is None

    await save_fair_value(
        FairValueRecord(**base, comps_detail_json='{"median_pe": 34.87}')  # type: ignore[arg-type]
    )
    second = get_fair_value("MU", "2026-10-13")
    assert second is not None
    assert second.comps_detail_json == '{"median_pe": 34.87}'
