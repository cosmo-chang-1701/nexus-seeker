"""單元測試：宏觀預期差計算服務 (macro_surprise_service.py)。"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, patch

import pytest
from market_analysis.fundamental_pipeline.macro_surprise import calculate_sample_std
from services.macro_surprise_service import (
    parse_calendar_metric_value,
    process_macro_surprises,
)


def test_parse_calendar_metric_value() -> None:
    """測試日曆字串指標解析為數值。"""
    # 百分比
    assert parse_calendar_metric_value("3.2%") == 3.2
    assert parse_calendar_metric_value("-0.4%") == -0.4
    assert parse_calendar_metric_value(" 0.0% ") == 0.0

    # 帶有 K / M / B 量綱
    assert parse_calendar_metric_value("250K") == 250000.0
    assert parse_calendar_metric_value("1.5M") == 1500000.0
    assert parse_calendar_metric_value("$4.2B") == 4200000000.0

    # 普通數值與逗號
    assert parse_calendar_metric_value("4.25") == 4.25
    assert parse_calendar_metric_value("1,250") == 1250.0

    # 空值與無效格式
    assert parse_calendar_metric_value(None) is None
    assert parse_calendar_metric_value("") is None
    assert parse_calendar_metric_value("-") is None
    assert parse_calendar_metric_value("N/A") is None
    assert parse_calendar_metric_value("nan") is None
    assert parse_calendar_metric_value("NaN") is None
    assert parse_calendar_metric_value("inf") is None


def _insert_calendar_event(
    db_conn: Any,
    event: str,
    event_time: str,
    consensus: str | None,
    actual: float | None,
) -> None:
    """依 economic_calendar_events 真實 schema 寫入：consensus_value TEXT、actual_value REAL。"""
    db_conn.execute(
        """
        INSERT INTO economic_calendar_events
        (month_key, event, event_time, impact, country, consensus_value, actual_value)
        VALUES (?, ?, ?, 'high', 'US', ?, ?)
        """,
        (event_time[:7], event, event_time, consensus, actual),
    )
    db_conn.commit()


def _insert_surprise(
    db_conn: Any, event_key: str, release_time: str, raw_diff: float
) -> None:
    db_conn.execute(
        """
        INSERT INTO macro_release_surprise
        (event_key, release_time_utc, actual, forecast, raw_diff, z_score, growth_sign)
        VALUES (?, ?, ?, 0.0, ?, NULL, -1)
        """,
        (event_key, release_time, raw_diff, raw_diff),
    )
    db_conn.commit()


@pytest.mark.asyncio
async def test_process_macro_surprises_real_calendar_rows(db_conn: Any) -> None:
    """以 Edge 翻譯後的中文事件名稱與 REAL 浮點數 actual_value 端到端計算並入庫。"""
    _insert_calendar_event(db_conn, "CPI 月增率", "2026-09-11T12:30:00Z", "0.2", 0.3)
    _insert_calendar_event(
        db_conn, "核心 CPI 月增率", "2026-09-11T12:30:00Z", "0.3", 0.4
    )
    _insert_calendar_event(
        db_conn, "非農就業人數", "2026-09-04T12:30:00Z", "180000", 220000.0
    )
    # 子字串相近但不在註冊表內的事件，不得被錯歸
    _insert_calendar_event(
        db_conn, "連續請領失業救濟金人數", "2026-09-10T12:30:00Z", "1950000", 1970000.0
    )
    # 尚未公布（actual 為 NULL）不處理
    _insert_calendar_event(db_conn, "PPI 月增率", "2026-09-12T12:30:00Z", "0.2", None)

    readings = await process_macro_surprises()
    keys = sorted(r.event_key for r in readings)
    assert keys == ["CORE_CPI_MOM", "CPI_MOM", "NFP"]

    cpi = next(r for r in readings if r.event_key == "CPI_MOM")
    assert cpi.actual == 0.3
    assert cpi.forecast == 0.2
    assert pytest.approx(cpi.raw_diff, 1e-4) == 0.1
    assert cpi.growth_sign == -1
    assert cpi.z_score is None  # 歷史樣本 < 6

    nfp = next(r for r in readings if r.event_key == "NFP")
    assert nfp.actual == 220000.0
    assert pytest.approx(nfp.raw_diff, 1e-4) == 40000.0

    # 同一發布時間的 CPI 與核心 CPI 各自獨立入庫，不互相覆寫
    rows = db_conn.execute(
        "SELECT event_key, actual FROM macro_release_surprise "
        "WHERE release_time_utc = '2026-09-11T12:30:00Z' ORDER BY event_key"
    ).fetchall()
    assert rows == [("CORE_CPI_MOM", 0.4), ("CPI_MOM", 0.3)]


@pytest.mark.asyncio
async def test_process_macro_surprises_uses_only_prior_history(db_conn: Any) -> None:
    """Z 分數只能使用事件「之前」的歷史樣本，事件之後才公布的資料不得混入。"""
    prior_diffs = [0.1, -0.1, 0.2, -0.2, 0.1, -0.1]
    for i, diff in enumerate(prior_diffs):
        _insert_surprise(db_conn, "CPI_MOM", f"2026-0{i + 1}-10T12:30:00Z", diff)
    # 事件之後的極端樣本（若被誤用會大幅拉高 sigma）
    for month in ("11", "12"):
        _insert_surprise(db_conn, "CPI_MOM", f"2026-{month}-10T12:30:00Z", 5.0)

    _insert_calendar_event(db_conn, "CPI 月增率", "2026-09-11T12:30:00Z", "0.2", 0.4)

    readings = await process_macro_surprises()
    assert len(readings) == 1
    reading = readings[0]

    expected_sigma = calculate_sample_std(prior_diffs)
    assert expected_sigma is not None
    expected_z = round(0.2 / expected_sigma, 4)
    assert reading.z_score is not None
    assert reading.z_score == pytest.approx(expected_z, abs=1e-4)


@pytest.mark.asyncio
async def test_process_macro_surprises_skips_recorded_events(db_conn: Any) -> None:
    """已入庫的發布不重算、不覆寫。"""
    db_conn.execute(
        """
        INSERT INTO macro_release_surprise
        (event_key, release_time_utc, actual, forecast, raw_diff, z_score, growth_sign)
        VALUES ('CPI_MOM', '2026-09-11T12:30:00Z', 0.3, 0.2, 0.1, 1.23, -1)
        """
    )
    db_conn.commit()
    _insert_calendar_event(db_conn, "CPI 月增率", "2026-09-11T12:30:00Z", "0.2", 0.9)

    readings = await process_macro_surprises()
    assert readings == []

    row = db_conn.execute(
        "SELECT actual, z_score FROM macro_release_surprise "
        "WHERE event_key = 'CPI_MOM' AND release_time_utc = '2026-09-11T12:30:00Z'"
    ).fetchone()
    assert row == (0.3, 1.23)


@pytest.mark.asyncio
async def test_process_macro_surprises_mocked() -> None:
    """以 mock 讀取層驗證：中文事件名稱、REAL 浮點數 actual 經 str() 後的解析，
    以及歷史查詢帶入事件發布時間作為上界。"""
    mock_events = [
        {
            "event": "CPI 月增率",
            "event_time": "2026-10-05T12:30:00Z",
            "consensus_value": "0.2",
            "actual_value": str(0.3),
            "country": "US",
        },
        {
            "event": "非農就業人數",
            "event_time": "2026-10-02T12:30:00Z",
            "consensus_value": "180K",
            "actual_value": str(220000.0),
            "country": "US",
        },
    ]

    with patch(
        "services.macro_surprise_service._load_calendar_events_with_actuals",
        return_value=mock_events,
    ), patch(
        "services.macro_surprise_service.get_recorded_macro_surprise_keys",
        return_value=set(),
    ), patch(
        "services.macro_surprise_service.get_macro_surprises_for_event",
        return_value=[],
    ) as mock_history, patch(
        "services.macro_surprise_service.save_macro_surprise",
        new_callable=AsyncMock,
    ) as mock_save:
        readings = await process_macro_surprises()
        assert len(readings) == 2

        cpi = next(r for r in readings if r.event_key == "CPI_MOM")
        assert cpi.actual == 0.3
        assert cpi.forecast == 0.2
        assert cpi.z_score is None

        nfp = next(r for r in readings if r.event_key == "NFP")
        assert nfp.actual == 220000.0
        assert nfp.forecast == 180000.0
        assert nfp.growth_sign == 1

        assert mock_save.await_count == 2
        called_befores = sorted(c.args[2] for c in mock_history.call_args_list)
        assert called_befores == ["2026-10-02T12:30:00Z", "2026-10-05T12:30:00Z"]
