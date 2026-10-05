"""單元測試：v093 遷移腳本與基本面 PR4 產業鏈交叉驗證資料庫讀寫 (test_channel_check_db.py)。"""

from __future__ import annotations

import json
import sqlite3

import pytest
from database.fundamental_pipeline import (
    get_channel_check,
    get_channel_checks_by_symbol,
    get_channel_checks_for_period,
    get_latest_channel_checks,
    save_channel_check_log,
    save_channel_check_logs,
)
from database.migrations import v093_add_channel_check_log as mig
from market_analysis.fundamental_pipeline.models import (
    ChannelCheckLogRecord,
)


def test_v093_migration_metadata() -> None:
    """驗證 v093 遷移腳本導出的常數與表格。"""
    assert hasattr(mig, "version")
    assert mig.version == 93
    assert hasattr(mig, "description")
    assert "channel_check_log" in mig.description
    assert hasattr(mig, "sql")
    assert "CREATE TABLE IF NOT EXISTS channel_check_log" in mig.sql
    assert "idx_channel_check_period" in mig.sql


def test_v093_migration_ddl_execution() -> None:
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
        assert "channel_check_log" in tables

        indices = [
            r[0]
            for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='index'"
            ).fetchall()
        ]
        assert "idx_channel_check_period" in indices
    finally:
        conn.close()


@pytest.mark.asyncio
async def test_database_channel_check_log_roundtrip() -> None:
    """測試 channel_check_log 單筆寫入、更新與查詢。"""
    record = ChannelCheckLogRecord(
        link_key="AI_CAPEX",
        as_of_period="2026-Q2",
        link_type="CAUSAL",
        experimental=False,
        driver_growth=28.5,
        follower_growth=24.0,
        divergence_pp=-4.5,
        nowcast_direction=None,
        nowcast_hit=None,
        correlation=None,
        verdict="CONFIRM",
        members_json=json.dumps({"drivers": ["MSFT"], "followers": ["NVDA"]}),
    )

    await save_channel_check_log(record)

    fetched = get_channel_check("AI_CAPEX", "2026-Q2")
    assert fetched is not None
    assert fetched.link_key == "AI_CAPEX"
    assert fetched.as_of_period == "2026-Q2"
    assert fetched.link_type == "CAUSAL"
    assert fetched.experimental is False
    assert fetched.driver_growth == 28.5
    assert fetched.follower_growth == 24.0
    assert fetched.divergence_pp == -4.5
    assert fetched.verdict == "CONFIRM"

    # 測試更新 (Upsert)
    updated = ChannelCheckLogRecord(
        link_key="AI_CAPEX",
        as_of_period="2026-Q2",
        link_type="CAUSAL",
        experimental=False,
        driver_growth=30.0,
        follower_growth=25.0,
        divergence_pp=-5.0,
        nowcast_direction=None,
        nowcast_hit=None,
        correlation=None,
        verdict="CONFIRM",
        members_json=json.dumps({"drivers": ["MSFT"], "followers": ["NVDA"]}),
    )
    await save_channel_check_log(updated)

    refetched = get_channel_check("AI_CAPEX", "2026-Q2")
    assert refetched is not None
    assert refetched.driver_growth == 30.0
    assert refetched.divergence_pp == -5.0


@pytest.mark.asyncio
async def test_database_channel_check_logs_batch_and_queries() -> None:
    """測試批次寫入與多維度日誌查詢 (週期、最新列表、標的篩選)。"""
    records = [
        ChannelCheckLogRecord(
            link_key="AIR_TRAVEL",
            as_of_period="2026-Q2",
            link_type="NOWCAST",
            experimental=False,
            driver_growth=4.5,
            follower_growth=3.8,
            divergence_pp=-0.7,
            nowcast_direction="NOWCAST_UP",
            nowcast_hit=True,
            correlation=0.92,
            verdict="CONFIRM",
            members_json=json.dumps({"followers": ["DAL", "UAL"]}),
        ),
        ChannelCheckLogRecord(
            link_key="SPACE_EO_DATA",
            as_of_period="2026-Q2",
            link_type="NOWCAST",
            experimental=True,
            driver_growth=-10.0,
            follower_growth=5.0,
            divergence_pp=15.0,
            nowcast_direction="NOWCAST_DOWN",
            nowcast_hit=False,
            correlation=-0.3,
            verdict="DIVERGE",
            members_json=json.dumps({"followers": ["PL", "BKSY"]}),
        ),
    ]

    await save_channel_check_logs(records)

    # 測試依週期查詢
    period_records = get_channel_checks_for_period("2026-Q2")
    assert len(period_records) >= 2
    keys = {r.link_key for r in period_records}
    assert "AIR_TRAVEL" in keys
    assert "SPACE_EO_DATA" in keys

    # 測試依 verdict 過濾查詢
    confirm_records = get_latest_channel_checks(verdict="CONFIRM")
    assert any(r.link_key == "AIR_TRAVEL" for r in confirm_records)

    diverge_records = get_latest_channel_checks(verdict="DIVERGE")
    assert any(r.link_key == "SPACE_EO_DATA" for r in diverge_records)

    # 測試依標的查詢 (DAL)
    dal_records = get_channel_checks_by_symbol("DAL")
    assert len(dal_records) >= 1
    assert dal_records[0].link_key == "AIR_TRAVEL"


@pytest.mark.asyncio
async def test_get_channel_checks_by_symbol_prefixed_and_tw() -> None:
    """測試包含前綴代碼 (XBRL:MSFT:capex, TWSE:2382) 之標的能被精確查詢。"""
    record = ChannelCheckLogRecord(
        link_key="AI_CAPEX",
        as_of_period="2026-Q3",
        link_type="CAUSAL",
        experimental=False,
        driver_growth=32.0,
        follower_growth=28.0,
        divergence_pp=-4.0,
        nowcast_direction=None,
        nowcast_hit=None,
        correlation=None,
        verdict="CONFIRM",
        members_json=json.dumps(
            {
                "drivers": ["XBRL:MSFT:capex", "XBRL:AMZN:capex"],
                "followers": ["NVDA", "TWSE:2382"],
                "symbols": ["MSFT", "AMZN", "NVDA", "2382"],
            }
        ),
    )
    await save_channel_check_log(record)

    # 1. 查詢驅動端 MSFT (原為 XBRL:MSFT:capex)
    msft_results = get_channel_checks_by_symbol("MSFT")
    assert any(r.link_key == "AI_CAPEX" for r in msft_results)

    # 2. 查詢台股代碼 2382 (原為 TWSE:2382)
    tw_results = get_channel_checks_by_symbol("2382")
    assert any(r.link_key == "AI_CAPEX" for r in tw_results)


@pytest.mark.asyncio
async def test_save_channel_check_logs_chunking() -> None:
    """測試超過 100 筆的大批次寫入經分塊處理無異常。"""
    large_batch = [
        ChannelCheckLogRecord(
            link_key=f"LINK_CHUNK_{i}",
            as_of_period="2026-Q1",
            link_type="CAUSAL",
            experimental=False,
            driver_growth=10.0,
            follower_growth=12.0,
            divergence_pp=2.0,
            nowcast_direction=None,
            nowcast_hit=None,
            correlation=None,
            verdict="CONFIRM",
            members_json="{}",
        )
        for i in range(125)
    ]
    await save_channel_check_logs(large_batch)

    # 驗證首尾與中間筆數均寫入成功
    assert get_channel_check("LINK_CHUNK_0", "2026-Q1") is not None
    assert get_channel_check("LINK_CHUNK_99", "2026-Q1") is not None
    assert get_channel_check("LINK_CHUNK_124", "2026-Q1") is not None
