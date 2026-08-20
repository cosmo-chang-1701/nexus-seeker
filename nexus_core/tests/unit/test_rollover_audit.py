"""Unit tests for the Dynamic Rollover Engine audit trail (rollover_audit_log)."""

from typing import Any

import pytest

from database.rollover_audit import get_rollover_audit_log, log_rollover_instruction
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


@pytest.mark.asyncio
async def test_get_rollover_audit_log_scoped_per_user_and_ordered(
    db_conn: Any,
) -> None:
    await log_rollover_instruction(
        user_id=1,
        symbol="AAA",
        scenario="OPPORTUNITY_COST",
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
    assert "尚無轉倉建議推送紀錄" in str(embed.description)


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
