"""單元測試：v091 遷移腳本與基本面 PR2 資料庫讀寫 (v091_add_sec_event_stream.py)。"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone
import pytest

from database.fundamental_pipeline import (
    get_active_governance_flags,
    get_insider_transactions,
    get_recent_sec_events,
    get_sec_filing_cursor,
    save_governance_flags,
    save_insider_transactions,
    save_sec_filing_event,
    save_sec_filing_events,
    upsert_sec_filing_cursor,
)
from database.migrations import v091_add_sec_event_stream as mig
from market_analysis.fundamental_pipeline.models import (
    FilingCursorRecord,
    FilingEventRecord,
    GovernanceFlagRecord,
    InsiderTxRecord,
)


def test_v091_migration_metadata() -> None:
    """驗證 v091 遷移腳本導出的常數與表格。"""
    assert hasattr(mig, "version")
    assert mig.version == 91
    assert hasattr(mig, "description")
    assert "sec_filing_cursor" in mig.description
    assert hasattr(mig, "sql")
    assert "CREATE TABLE IF NOT EXISTS sec_filing_cursor" in mig.sql
    assert "CREATE TABLE IF NOT EXISTS sec_filing_event" in mig.sql
    assert "CREATE TABLE IF NOT EXISTS insider_transaction" in mig.sql
    assert "CREATE TABLE IF NOT EXISTS governance_flag" in mig.sql


def test_v091_migration_ddl_execution() -> None:
    """測試 DDL 可於乾淨的 SQLite 連線上正確執行。"""
    conn = sqlite3.connect(":memory:")
    try:
        conn.executescript(mig.sql)
        tables = [
            r[0]
            for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        ]
        assert "sec_filing_cursor" in tables
        assert "sec_filing_event" in tables
        assert "insider_transaction" in tables
        assert "governance_flag" in tables
    finally:
        conn.close()


@pytest.mark.asyncio
async def test_database_cursor_roundtrip() -> None:
    """測試 SEC 申報游標寫入與查詢。"""
    cursor = FilingCursorRecord(
        symbol="TSLA",
        cik="0001318605",
        last_accepted_at="2026-10-05 16:45:00",
        last_accession="0001318605-26-000020",
    )
    await upsert_sec_filing_cursor(cursor)

    fetched = get_sec_filing_cursor("TSLA")
    assert fetched is not None
    assert fetched.symbol == "TSLA"
    assert fetched.cik == "0001318605"
    assert fetched.last_accepted_at == "2026-10-05 16:45:00"
    assert fetched.last_accession == "0001318605-26-000020"


@pytest.mark.asyncio
async def test_database_sec_filing_events_roundtrip() -> None:
    """測試 SEC 申報事件寫入與查詢。"""
    event = FilingEventRecord(
        accession="0001045810-26-000001",
        symbol="NVDA",
        form="8-K",
        items="2.02,4.02",
        accepted_at="2026-10-05 17:00:00",
        session="AMC",
        primary_doc_url="https://sec.gov/doc.htm",
        routes_json='["EARNINGS", "GOVERNANCE_CRITICAL"]',
        is_backfill=False,
    )
    await save_sec_filing_event(event)

    event2 = FilingEventRecord(
        accession="0001045810-26-000002",
        symbol="NVDA",
        form="4",
        items=None,
        accepted_at="2026-10-05 17:05:00",
        session="AMC",
        primary_doc_url="https://sec.gov/doc2.xml",
        routes_json='["INSIDER_TRANSACTION"]',
        is_backfill=False,
    )
    await save_sec_filing_events([event2])

    events = get_recent_sec_events("NVDA", limit=5)
    assert len(events) >= 2
    ev = next(e for e in events if e.accession == "0001045810-26-000001")
    assert ev.symbol == "NVDA"
    assert ev.form == "8-K"
    assert ev.session == "AMC"
    assert ev.is_backfill is False
    ev2 = next(e for e in events if e.accession == "0001045810-26-000002")
    assert ev2.form == "4"


@pytest.mark.asyncio
async def test_database_insider_transactions_roundtrip() -> None:
    """測試內部人交易明細批次寫入與查詢。"""
    now_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    tx = InsiderTxRecord(
        accession="0001318605-26-000099",
        line_no=1,
        symbol="TSLA",
        owner_name="Elon Musk",
        owner_role="CEO",
        is_c_suite=True,
        tx_date=now_str,
        tx_code="P",
        shares=1000.0,
        price=200.0,
        acquired_disposed="A",
        shares_after=100000.0,
        is_10b5_1=False,
        is_backfill=False,
    )
    await save_insider_transactions([tx])

    records = get_insider_transactions("TSLA", days=30)
    assert len(records) >= 1
    r = next(r for r in records if r.accession == "0001318605-26-000099")
    assert r.owner_name == "Elon Musk"
    assert r.is_c_suite is True
    assert r.shares == 1000.0
    assert r.price == 200.0


@pytest.mark.asyncio
async def test_database_governance_flags_roundtrip() -> None:
    """測試治理審查旗標寫入與生效中查詢。"""
    now_dt = datetime.now(timezone.utc)
    future_exp = (now_dt + timedelta(days=20)).strftime("%Y-%m-%d %H:%M:%S")

    flag = GovernanceFlagRecord(
        symbol="SMCI",
        source_accession="0001375365-26-000001",
        flag_kind="ITEM_4_02_RESTATEMENT",
        severity="CRITICAL",
        detail_json='{"reason": "Restatement"}',
        expires_at=future_exp,
    )
    await save_governance_flags([flag])

    active = get_active_governance_flags("SMCI")
    assert len(active) >= 1
    act = next(f for f in active if f.source_accession == "0001375365-26-000001")
    assert act.severity == "CRITICAL"
    assert act.flag_kind == "ITEM_4_02_RESTATEMENT"


@pytest.mark.asyncio
async def test_insider_owner_cik_and_amendment_roundtrip() -> None:
    """主申報人 CIK 編碼於 owner_name 欄（不改 schema）且可還原；4/A 由事件表 form 還原。"""
    now_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    await save_sec_filing_events(
        [
            FilingEventRecord(
                accession="0009999999-26-000401",
                symbol="ZZCK",
                form="4/A",
                items=None,
                accepted_at="2026-10-05T18:42:45-04:00",
                session="AMC",
            )
        ]
    )
    await save_insider_transactions(
        [
            InsiderTxRecord(
                accession="0009999999-26-000401",
                line_no=1,
                symbol="ZZCK",
                owner_name="Huang Family Trust, HUANG JENSEN",
                owner_cik="0001197649",
                tx_date=now_str,
                tx_code="P",
                shares=10.0,
                price=1.0,
                acquired_disposed="A",
                is_amendment=True,
            ),
            InsiderTxRecord(
                accession="0009999999-26-000402",
                line_no=1,
                symbol="ZZCK",
                owner_name="NO CIK OWNER",
                tx_date=now_str,
                tx_code="P",
                shares=5.0,
                price=1.0,
                acquired_disposed="A",
            ),
        ]
    )

    records = {r.accession: r for r in get_insider_transactions("ZZCK", days=30)}
    amended = records["0009999999-26-000401"]
    assert amended.owner_name == "Huang Family Trust, HUANG JENSEN"
    assert amended.owner_cik == "0001197649"
    assert amended.owner_key == "CIK:0001197649"
    assert amended.is_amendment is True
    assert amended.filing_accepted_at == "2026-10-05T18:42:45-04:00"

    plain = records["0009999999-26-000402"]
    assert plain.owner_name == "NO CIK OWNER"
    assert plain.owner_cik is None
    assert plain.is_amendment is False
