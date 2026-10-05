"""單元測試：v094 遷移腳本與資料庫讀寫功能 (v094_add_valuation_and_watch.py)。"""

from __future__ import annotations

import sqlite3
from typing import Any

import pytest
from database.fundamental_pipeline import (
    get_fair_value,
    get_latest_fair_value,
    get_latest_revision_score,
    get_latest_watch_candidates,
    get_revision_score,
    get_watch_candidates,
    save_fair_value,
    save_fair_values,
    save_revision_score,
    save_revision_scores,
    save_watch_candidates,
)
from database.migrations import v094_add_valuation_and_watch as mig
from market_analysis.fundamental_pipeline.models import (
    FairValueRecord,
    RevisionScoreRecord,
    WatchCandidateRecord,
)


def test_v094_migration_metadata() -> None:
    """驗證遷移腳本導出的模組層級常數。"""
    assert hasattr(mig, "version")
    assert mig.version == 94
    assert hasattr(mig, "description")
    assert "revision_score_log" in mig.description
    assert hasattr(mig, "sql")
    assert "CREATE TABLE IF NOT EXISTS revision_score_log" in mig.sql
    assert "CREATE TABLE IF NOT EXISTS fair_value_log" in mig.sql
    assert "CREATE TABLE IF NOT EXISTS fundamental_watch_candidate" in mig.sql


def test_v094_migration_ddl_execution() -> None:
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
        assert "revision_score_log" in tables
        assert "fair_value_log" in tables
        assert "fundamental_watch_candidate" in tables

        # 測試 status check constraint
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                """
                INSERT INTO fundamental_watch_candidate (
                    trading_date, symbol, rank, status, reasons_json
                ) VALUES ('2026-10-05', 'AAPL', 1, 'INVALID_STATUS', '{}')
                """
            )
    finally:
        conn.close()


@pytest.mark.asyncio
async def test_database_valuation_and_watch_roundtrip(db_conn: Any) -> None:
    """測試 database.fundamental_pipeline 的 PR5 資料庫讀寫與更新。"""
    date_str = "2026-10-05"

    # 1. 修正動能讀寫
    rev_rec = RevisionScoreRecord(
        symbol="AAPL",
        trading_date=date_str,
        score_30d=45.5,
        breadth_ratio=0.75,
        is_pead_aligned=True,
        detail_json='{"up": 3, "down": 1}',
    )
    await save_revision_score(rev_rec)

    fetched_rev = get_revision_score("AAPL", date_str)
    assert fetched_rev is not None
    assert fetched_rev.symbol == "AAPL"
    assert fetched_rev.score_30d == 45.5
    assert fetched_rev.breadth_ratio == 0.75
    assert fetched_rev.is_pead_aligned is True

    latest_rev = get_latest_revision_score("AAPL")
    assert latest_rev is not None
    assert latest_rev.score_30d == 45.5

    # 2. 公允價值讀寫
    fv_rec = FairValueRecord(
        symbol="AAPL",
        trading_date=date_str,
        dcf_value=210.0,
        comps_value=195.0,
        fair_value=202.5,
        margin_of_safety=0.15,
        discount_rate=0.082,
        equity_risk_premium=0.041,
        flags_json='["MODERATE_DISCOUNT"]',
    )
    await save_fair_value(fv_rec)

    fetched_fv = get_fair_value("AAPL", date_str)
    assert fetched_fv is not None
    assert fetched_fv.fair_value == 202.5
    assert fetched_fv.dcf_value == 210.0
    assert fetched_fv.comps_value == 195.0
    assert fetched_fv.margin_of_safety == 0.15

    latest_fv = get_latest_fair_value("AAPL")
    assert latest_fv is not None
    assert latest_fv.fair_value == 202.5

    # 3. 基本面次日候選名單讀寫
    candidates = [
        WatchCandidateRecord(
            trading_date=date_str,
            symbol="NVDA",
            rank=1,
            status="CANDIDATE",
            reasons_json='{"composite_score": 65.0}',
        ),
        WatchCandidateRecord(
            trading_date=date_str,
            symbol="AAPL",
            rank=2,
            status="WATCH",
            reasons_json='{"composite_score": 30.0}',
        ),
        WatchCandidateRecord(
            trading_date=date_str,
            symbol="SMCI",
            rank=3,
            status="EXCLUDED",
            reasons_json="{}",
            excluded_reason="CRITICAL_GOVERNANCE_FLAG",
        ),
    ]
    await save_watch_candidates(candidates)

    all_cands = get_watch_candidates(date_str)
    assert len(all_cands) == 3
    assert all_cands[0].symbol == "NVDA"
    assert all_cands[0].status == "CANDIDATE"

    only_candidates = get_watch_candidates(date_str, status="CANDIDATE")
    assert len(only_candidates) == 1
    assert only_candidates[0].symbol == "NVDA"

    latest_cands = get_latest_watch_candidates(limit=2)
    assert len(latest_cands) == 2
    assert latest_cands[0].rank == 1


@pytest.mark.asyncio
async def test_batch_save_valuation_records(db_conn: Any) -> None:
    """測試批次寫入 save_revision_scores 與 save_fair_values。"""
    date_str = "2026-10-06"
    rev_batch = [
        RevisionScoreRecord(
            symbol="MSFT",
            trading_date=date_str,
            score_30d=20.0,
            breadth_ratio=0.5,
            is_pead_aligned=False,
            detail_json="{}",
        ),
        RevisionScoreRecord(
            symbol="GOOGL",
            trading_date=date_str,
            score_30d=-10.0,
            breadth_ratio=-0.2,
            is_pead_aligned=False,
            detail_json="{}",
        ),
    ]
    await save_revision_scores(rev_batch)

    assert get_revision_score("MSFT", date_str) is not None
    assert get_revision_score("GOOGL", date_str) is not None

    fv_batch = [
        FairValueRecord(
            symbol="MSFT",
            trading_date=date_str,
            dcf_value=450.0,
            comps_value=440.0,
            fair_value=445.0,
            margin_of_safety=0.10,
            discount_rate=0.08,
            equity_risk_premium=0.045,
            flags_json="[]",
        ),
        FairValueRecord(
            symbol="GOOGL",
            trading_date=date_str,
            dcf_value=190.0,
            comps_value=180.0,
            fair_value=185.0,
            margin_of_safety=0.05,
            discount_rate=0.08,
            equity_risk_premium=0.045,
            flags_json="[]",
        ),
    ]
    await save_fair_values(fv_batch)

    assert get_fair_value("MSFT", date_str) is not None
    assert get_fair_value("GOOGL", date_str) is not None
