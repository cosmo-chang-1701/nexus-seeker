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
from database.core import get_migrations
from database.migrations import v092_add_earnings_surprise as mig_v092
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


def test_migrations_from_v090_are_contiguous() -> None:
    """v090 起的遷移版本必須連續且唯一（runner 只執行 > MAX(version)，跳號會讓後續版本被誤判）。"""
    versions = [m["version"] for m in get_migrations() if m["version"] >= 90]
    assert versions == list(range(90, max(versions) + 1))
    assert len(versions) == len(set(versions))
    assert max(versions) == 94


def _columns(conn: sqlite3.Connection, table: str) -> dict[str, int]:
    """回傳 {欄位名: notnull 旗標}。"""
    rows = conn.execute(
        'SELECT name, "notnull" FROM pragma_table_info(?)', (table,)
    ).fetchall()
    return {r[0]: int(r[1]) for r in rows}


def test_v094_migration_ddl_execution() -> None:
    """DDL 可於套用 v092 後的 SQLite 連線上正確執行（v094 會 ALTER v092 的兩張表）。"""
    conn = sqlite3.connect(":memory:")
    try:
        conn.executescript(mig_v092.sql)
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


def test_v094_final_schema_nullable_and_new_columns() -> None:
    """無效值存 NULL（不以 0.0 哨兵）；v092 兩張表以 ADD COLUMN 補財期與發布日。"""
    conn = sqlite3.connect(":memory:")
    try:
        conn.executescript(mig_v092.sql)
        conn.executescript(mig.sql)
        fv_cols = _columns(conn, "fair_value_log")
        assert fv_cols["fair_value"] == 0
        assert fv_cols["margin_of_safety"] == 0
        assert fv_cols["method"] == 1
        assert "spot_price" in fv_cols
        rev_cols = _columns(conn, "revision_score_log")
        assert rev_cols["score_30d"] == 0
        assert rev_cols["breadth_ratio"] == 0
        assert "fiscal_period" in _columns(conn, "eps_estimate_snapshot")
        assert "announced_on" in _columns(conn, "earnings_surprise")

        conn.execute(
            """
            INSERT INTO fair_value_log (
                symbol, trading_date, fair_value, margin_of_safety,
                discount_rate, equity_risk_premium, flags_json
            ) VALUES ('SPY', '2026-10-05', NULL, NULL, 0.08, 0.045, '[]')
            """
        )
        row = conn.execute(
            "SELECT fair_value, margin_of_safety, method FROM fair_value_log"
        ).fetchone()
        assert row == (None, None, "NONE")
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                """
                INSERT INTO fair_value_log (
                    symbol, trading_date, discount_rate, equity_risk_premium,
                    flags_json, method
                ) VALUES ('X', '2026-10-05', 0.08, 0.045, '[]', 'BOGUS')
                """
            )
    finally:
        conn.close()


@pytest.mark.asyncio
async def test_null_fair_value_and_revision_roundtrip(db_conn: Any) -> None:
    """method = NONE 的估值與無可配對財期的動能以 NULL 入庫並原樣讀回。"""
    await save_fair_value(
        FairValueRecord(
            symbol="SPY",
            trading_date="2026-10-05",
            dcf_value=None,
            comps_value=None,
            fair_value=None,
            margin_of_safety=None,
            discount_rate=0.08,
            equity_risk_premium=0.045,
            flags_json='["FCF_UNAVAILABLE"]',
            method="NONE",
            spot_price=570.0,
        )
    )
    fv = get_fair_value("SPY", "2026-10-05")
    assert fv is not None
    assert fv.fair_value is None
    assert fv.margin_of_safety is None
    assert fv.method == "NONE"
    assert fv.spot_price == 570.0

    await save_revision_score(
        RevisionScoreRecord(
            symbol="SPY",
            trading_date="2026-10-05",
            score_30d=None,
            breadth_ratio=None,
            is_pead_aligned=False,
            detail_json="{}",
        )
    )
    rev = get_revision_score("SPY", "2026-10-05")
    assert rev is not None
    assert rev.score_30d is None
    assert rev.breadth_ratio is None


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
