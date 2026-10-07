"""單元測試：v092 遷移腳本與基本面 PR3 資料庫讀寫 (test_earnings_surprise_db.py)。"""

from __future__ import annotations

import sqlite3

import pytest
from database.fundamental_pipeline import (
    get_earnings_surprise,
    get_eps_estimate_snapshots,
    get_guidance_extraction,
    get_latest_earnings_surprise,
    get_latest_guidance_extraction,
    save_earnings_surprise,
    save_earnings_surprises,
    save_eps_estimate_snapshots,
    save_guidance_extraction,
)
from database.migrations import v092_add_earnings_surprise as mig
from market_analysis.fundamental_pipeline.models import (
    EarningsSurpriseDTO,
    EPSEstimateSnapshotRecord,
    GuidanceExtractionDTO,
)


def test_v092_migration_metadata() -> None:
    """驗證 v092 遷移腳本導出的常數與表格。"""
    assert hasattr(mig, "version")
    assert mig.version == 92
    assert hasattr(mig, "description")
    assert "earnings_surprise" in mig.description
    assert hasattr(mig, "sql")
    assert "CREATE TABLE IF NOT EXISTS earnings_surprise" in mig.sql
    assert "CREATE TABLE IF NOT EXISTS eps_estimate_snapshot" in mig.sql
    assert "CREATE TABLE IF NOT EXISTS guidance_extraction" in mig.sql


def test_v092_migration_ddl_execution() -> None:
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
        assert "earnings_surprise" in tables
        assert "eps_estimate_snapshot" in tables
        assert "guidance_extraction" in tables
    finally:
        conn.close()


@pytest.mark.asyncio
async def test_database_earnings_surprise_roundtrip() -> None:
    """測試 earnings_surprise 寫入、更新與查詢。"""
    surprise1 = EarningsSurpriseDTO(
        symbol="MSFT",
        fiscal_period="2026-Q1",
        actual_eps=3.20,
        consensus_eps=3.10,
        eps_surprise_pct=0.032,
        actual_revenue=65000000000.0,
        consensus_revenue=64500000000.0,
        revenue_surprise_pct=0.0078,
        whisper_eps=3.15,
        composite_score=12.5,
        session="AMC",
        eps_basis="VENDOR_ADJUSTED",
        status="PROCESSED",
    )
    await save_earnings_surprise(surprise1)

    fetched = get_earnings_surprise("MSFT", "2026-Q1")
    assert fetched is not None
    assert fetched.symbol == "MSFT"
    assert fetched.fiscal_period == "2026-Q1"
    assert fetched.actual_eps == 3.20
    assert fetched.consensus_eps == 3.10
    assert fetched.composite_score == 12.5
    assert fetched.session == "AMC"
    assert fetched.status == "PROCESSED"

    # 測試 latest
    latest = get_latest_earnings_surprise("MSFT")
    assert latest is not None
    assert latest.fiscal_period == "2026-Q1"


@pytest.mark.asyncio
async def test_database_earnings_surprises_batch() -> None:
    """測試批次寫入 save_earnings_surprises。"""
    surprises = [
        EarningsSurpriseDTO(
            symbol="AMZN",
            fiscal_period="2026-Q1",
            actual_eps=1.25,
            consensus_eps=1.20,
            composite_score=10.0,
        ),
        EarningsSurpriseDTO(
            symbol="AMZN",
            fiscal_period="2026-Q2",
            actual_eps=1.40,
            consensus_eps=1.30,
            composite_score=20.0,
        ),
    ]
    await save_earnings_surprises(surprises)

    latest = get_latest_earnings_surprise("AMZN")
    assert latest is not None
    assert latest.fiscal_period == "2026-Q2"
    assert latest.actual_eps == 1.40


@pytest.mark.asyncio
async def test_database_eps_estimate_snapshots_roundtrip() -> None:
    """測試 eps_estimate_snapshot 批次寫入與查詢。"""
    snapshots = [
        EPSEstimateSnapshotRecord(
            symbol="GOOGL",
            snapshot_date="2026-10-05",
            horizon="0q",
            source="finnhub",
            eps_mean=1.85,
            eps_high=1.95,
            eps_low=1.75,
            analyst_count=32,
        ),
        EPSEstimateSnapshotRecord(
            symbol="GOOGL",
            snapshot_date="2026-10-05",
            horizon="+1q",
            source="finnhub",
            eps_mean=2.00,
            eps_high=2.10,
            eps_low=1.90,
            analyst_count=30,
        ),
    ]
    await save_eps_estimate_snapshots(snapshots)

    fetched = get_eps_estimate_snapshots("GOOGL")
    assert len(fetched) == 2
    assert fetched[0].horizon == "0q"
    assert fetched[0].eps_mean == 1.85
    assert fetched[1].horizon == "+1q"
    assert fetched[1].eps_mean == 2.00


@pytest.mark.asyncio
async def test_database_guidance_extraction_roundtrip() -> None:
    """測試 guidance_extraction 寫入與查詢。"""
    extraction = GuidanceExtractionDTO(
        symbol="META",
        fiscal_period="2026-Q3",
        source_accession="ACC-META-99",
        model_version="gpt-4o",
        confidence_score=0.92,
        tone_delta_score=18.0,
        data_json='{"backlog": 1}',
    )
    await save_guidance_extraction(extraction)

    fetched = get_guidance_extraction("META", "2026-Q3")
    assert fetched is not None
    assert fetched.symbol == "META"
    assert fetched.confidence_score == 0.92
    assert fetched.tone_delta_score == 18.0

    latest = get_latest_guidance_extraction("META")
    assert latest is not None
    assert latest.fiscal_period == "2026-Q3"


@pytest.mark.asyncio
async def test_get_prior_guidance_extraction_excludes_same_newer_and_unnormalized() -> (
    None
):
    """前期指引只取「早於當期財季」且非同一申報之記錄，並忽略未正規化之期別字串。"""
    from database.fundamental_pipeline import get_prior_guidance_extraction

    def _g(period: str, accession: str) -> GuidanceExtractionDTO:
        return GuidanceExtractionDTO(
            symbol="AMZN",
            fiscal_period=period,
            source_accession=accession,
            model_version="gpt-4o",
            confidence_score=0.5,
            tone_delta_score=0.0,
            data_json="{}",
        )

    for dto in (
        _g("2026-Q1", "ACC-Q1"),
        _g("2026-Q2", "ACC-Q2"),
        _g("2026-Q3", "ACC-Q3"),
        _g("2026-Q4", "ACC-Q4"),  # 較新季度不得被當成前期
        _g("Q3 2026", "ACC-LEGACY"),  # 未正規化字串（字串序會排在最後）
    ):
        await save_guidance_extraction(dto)

    prior = get_prior_guidance_extraction("AMZN", "2026-Q3", "ACC-Q3")
    assert prior is not None
    assert prior.fiscal_period == "2026-Q2"

    # 同一申報重送（例如修正後以相同 accession 寫入較早期別）也被排除
    prior_excl = get_prior_guidance_extraction("AMZN", "2026-Q3", "ACC-Q2")
    assert prior_excl is not None
    assert prior_excl.fiscal_period == "2026-Q1"

    assert get_prior_guidance_extraction("AMZN", "2026-Q1", "ACC-Q1") is None
