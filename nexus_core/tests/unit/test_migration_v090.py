"""單元測試：v090 遷移腳本與資料庫讀寫功能 (v090_add_liquidity_and_macro_surprise.py)。"""

from __future__ import annotations

import sqlite3
from datetime import date

import pytest
from database.fundamental_pipeline import (
    get_latest_liquidity_regime,
    get_latest_macro_surprises,
    get_liquidity_regime_by_date,
    get_macro_surprises_for_event,
    save_liquidity_regime,
    save_macro_surprise,
    save_macro_surprises,
)
from database.migrations import v090_add_liquidity_and_macro_surprise as mig
from market_analysis.fundamental_pipeline.models import (
    LiquidityReading,
    MacroSurpriseReading,
)


def test_v090_migration_metadata() -> None:
    """驗證遷移腳本導出的模組層級常數。"""
    assert hasattr(mig, "version")
    assert mig.version == 90
    assert hasattr(mig, "description")
    assert "liquidity_regime_log" in mig.description
    assert hasattr(mig, "sql")
    assert "CREATE TABLE IF NOT EXISTS liquidity_regime_log" in mig.sql
    assert "CREATE TABLE IF NOT EXISTS macro_release_surprise" in mig.sql


def test_v090_migration_ddl_execution() -> None:
    """測試 DDL 可於乾淨的 SQLite 連線上正確執行。"""
    conn = sqlite3.connect(":memory:")
    try:
        conn.executescript(mig.sql)
        # 驗證表格存在
        tables = [
            r[0]
            for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        ]
        assert "liquidity_regime_log" in tables
        assert "macro_release_surprise" in tables
    finally:
        conn.close()


@pytest.mark.asyncio
async def test_database_fundamental_pipeline_roundtrip() -> None:
    """測試 database.fundamental_pipeline 的讀寫與更新。"""
    today = date(2026, 10, 5)
    reading = LiquidityReading(
        trading_date=today,
        nfci=-0.54,
        anfci=-0.56,
        net_liquidity_bn=5850.5,
        net_liquidity_chg_13w_pct=2.45,
        reserves_chg_13w_pct=1.12,
        us10y=4.12,
        regime="EASY",
        equity_risk_premium=0.0396,
    )

    await save_liquidity_regime(reading)

    fetched = get_latest_liquidity_regime()
    assert fetched is not None
    assert fetched.trading_date == today
    assert fetched.regime == "EASY"
    assert fetched.net_liquidity_bn == 5850.5

    by_date = get_liquidity_regime_by_date(today)
    assert by_date is not None
    assert by_date.nfci == -0.54

    surprise = MacroSurpriseReading(
        event_key="CPI_MOM",
        release_time_utc="2026-10-05T12:30:00Z",
        actual=0.3,
        forecast=0.2,
        raw_diff=0.1,
        z_score=0.85,
        growth_sign=-1,
    )
    await save_macro_surprise(surprise)

    surprises = get_macro_surprises_for_event("CPI_MOM", limit=5)
    assert len(surprises) >= 1
    assert any(s.release_time_utc == "2026-10-05T12:30:00Z" for s in surprises)

    latest_surprises = get_latest_macro_surprises(limit=10)
    assert len(latest_surprises) >= 1


@pytest.mark.asyncio
async def test_save_macro_surprises_batch_execution() -> None:
    """測試 save_macro_surprises 批次多筆寫入與衝突更新。"""
    readings = [
        MacroSurpriseReading(
            event_key="NFP",
            release_time_utc="2026-09-04T12:30:00Z",
            actual=142000.0,
            forecast=160000.0,
            raw_diff=-18000.0,
            z_score=-0.45,
            growth_sign=1,
        ),
        MacroSurpriseReading(
            event_key="NFP",
            release_time_utc="2026-10-02T12:30:00Z",
            actual=254000.0,
            forecast=150000.0,
            raw_diff=104000.0,
            z_score=2.15,
            growth_sign=1,
        ),
    ]

    # 若 is_many 錯誤設定為 False，此處將觸發 sqlite3.ProgrammingError
    await save_macro_surprises(readings)

    fetched = get_macro_surprises_for_event("NFP", limit=5)
    assert len(fetched) >= 2
    nfp_oct = next(r for r in fetched if r.release_time_utc == "2026-10-02T12:30:00Z")
    assert nfp_oct.actual == 254000.0
    assert nfp_oct.z_score == 2.15
