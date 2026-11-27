"""Unit tests for the Dynamic Rollover Engine audit trail (rollover_audit_log)."""

from typing import Any

import pytest

from database.core import get_migrations
from database.migrations import v099_add_rollover_audit_delivery_status as mig_v099
from database.rollover_audit import (
    DELIVERY_DRY_RUN,
    DELIVERY_NOT_QUEUED,
    DELIVERY_QUEUED,
    get_rollover_audit_log,
    log_rollover_instruction,
)
from cogs.embed_builders.rollover_embeds import create_rollover_history_embed


@pytest.fixture(autouse=True)
def clean_audit_log(db_conn: Any) -> Any:
    cursor = db_conn.cursor()
    cursor.execute("DELETE FROM rollover_audit_log")
    db_conn.commit()
    yield


@pytest.mark.asyncio
async def test_log_rollover_instruction_persists_and_reads_back(db_conn: Any) -> None:
    await log_rollover_instruction(
        user_id=999222,
        symbol="nvda",
        scenario="MARGIN_DEFENSE",
        action="LIQUIDATE",
        sell_ratio=1.0,
        target_core="BOXX",
        suggested_price="$101.23 (限價)",
        cash_impact="$5,000",
    )

    records = get_rollover_audit_log(999222)
    assert len(records) == 1
    assert records[0]["symbol"] == "NVDA"
    assert records[0]["scenario"] == "MARGIN_DEFENSE"
    assert records[0]["action"] == "LIQUIDATE"
    assert records[0]["sell_ratio"] == 1.0
    assert records[0]["target_core"] == "BOXX"
    assert records[0]["created_at"] is not None
    # 未提供推播狀態時維持 NULL（與 v099 以前的舊列相同）
    assert records[0]["delivery_status"] is None


@pytest.mark.asyncio
async def test_get_rollover_audit_log_scoped_per_user_and_ordered(
    db_conn: Any,
) -> None:
    await log_rollover_instruction(
        user_id=1,
        symbol="AAA",
        scenario="TRANSITION_ENGINE",
        action="REDUCE",
        sell_ratio=0.5,
    )
    await log_rollover_instruction(
        user_id=1,
        symbol="BBB",
        scenario="SATELLITE_REBALANCE",
        action="LIQUIDATE",
        sell_ratio=1.0,
    )
    await log_rollover_instruction(
        user_id=2,
        symbol="CCC",
        scenario="MARGIN_DEFENSE",
        action="LIQUIDATE",
        sell_ratio=1.0,
    )

    user1_records = get_rollover_audit_log(1)
    assert len(user1_records) == 2
    assert {r["symbol"] for r in user1_records} == {"AAA", "BBB"}

    user2_records = get_rollover_audit_log(2)
    assert len(user2_records) == 1
    assert user2_records[0]["symbol"] == "CCC"


@pytest.mark.asyncio
async def test_get_rollover_audit_log_respects_limit(db_conn: Any) -> None:
    for i in range(5):
        await log_rollover_instruction(
            user_id=42,
            symbol=f"SYM{i}",
            scenario="SATELLITE_REBALANCE",
            action="HOLD",
            sell_ratio=0.0,
        )

    records = get_rollover_audit_log(42, limit=3)
    assert len(records) == 3


def test_create_rollover_history_embed_empty_state() -> None:
    embed = create_rollover_history_embed([])
    assert "尚無轉倉建議紀錄" in str(embed.description)


def test_create_rollover_history_embed_renders_records() -> None:
    records = [
        {
            "symbol": "NVDA",
            "scenario": "MARGIN_DEFENSE",
            "action": "LIQUIDATE",
            "sell_ratio": 1.0,
            "target_core": "BOXX",
            "suggested_price": "$101.23 (限價)",
            "cash_impact": "$5,000",
            "created_at": "2026-08-20 10:00:00",
        }
    ]
    embed = create_rollover_history_embed(records)
    field_value = str(embed.fields[0].value)
    assert "NVDA" in field_value
    assert "保證金防禦" in field_value
    assert "LIQUIDATE" in field_value
    assert "100%" in field_value


def test_migration_v099_module() -> None:
    assert mig_v099.version == 99
    assert mig_v099.description
    assert "ADD COLUMN delivery_status TEXT" in mig_v099.sql
    assert 99 in [m["version"] for m in get_migrations()]


@pytest.mark.asyncio
async def test_log_rollover_instruction_persists_delivery_status(db_conn: Any) -> None:
    await log_rollover_instruction(
        user_id=7,
        symbol="AAA",
        scenario="SHORT_ENTRY",
        action="OPEN_SHORT",
        sell_ratio=0.0,
        delivery_status=DELIVERY_DRY_RUN,
    )

    assert get_rollover_audit_log(7)[0]["delivery_status"] == "DRY_RUN"


@pytest.mark.parametrize(
    "status,label",
    [
        (DELIVERY_QUEUED, "已發"),
        (DELIVERY_DRY_RUN, "乾跑"),
        (DELIVERY_NOT_QUEUED, "未發"),
        (None, "未知"),
        ("SOMETHING_ELSE", "未知"),
    ],
)
def test_rollover_history_embed_delivery_column(status: str | None, label: str) -> None:
    embed = create_rollover_history_embed(
        [
            {
                "symbol": "NVDA",
                "scenario": "MARGIN_DEFENSE",
                "action": "REDUCE",
                "sell_ratio": 0.5,
                "delivery_status": status,
                "created_at": "2026-08-20 10:00:00",
            }
        ]
    )
    lines = str(embed.fields[0].value).split("\n")[1:-1]
    header, row = lines[0], lines[-1]
    assert header.split("|")[-1].strip() == "推播"
    assert row.split("|")[-1].strip() == label
    # 時間欄省略年份
    assert row.split("|")[0].strip() == "08-20 10:00"
