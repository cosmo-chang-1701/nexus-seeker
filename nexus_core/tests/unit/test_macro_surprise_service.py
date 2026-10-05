"""單元測試：宏觀預期差計算服務 (macro_surprise_service.py)。"""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest
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


@pytest.mark.asyncio
async def test_process_macro_surprises_mocked() -> None:
    """測試掃描日曆事件並產出宏觀預期差讀數。"""
    mock_events = [
        {
            "event": "CPI MoM",
            "event_time": "2026-10-05T12:30:00Z",
            "consensus_value": "0.2%",
            "actual_value": "0.3%",
            "country": "US",
        },
        {
            "event": "Non Farm Payrolls",
            "event_time": "2026-10-02T12:30:00Z",
            "consensus_value": "180K",
            "actual_value": "220K",
            "country": "US",
        },
    ]

    with patch(
        "services.macro_surprise_service._load_calendar_events_with_actuals",
        return_value=mock_events,
    ), patch(
        "services.macro_surprise_service.get_macro_surprises_for_event",
        return_value=[],
    ), patch(
        "services.macro_surprise_service.save_macro_surprise",
        new_callable=AsyncMock,
    ) as mock_save:
        readings = await process_macro_surprises()
        assert len(readings) == 2

        # 檢驗 CPI
        cpi = next(r for r in readings if r.event_key == "CPI_MOM")
        assert cpi.actual == 0.3
        assert cpi.forecast == 0.2
        assert pytest.approx(cpi.raw_diff, 1e-4) == 0.1
        assert cpi.growth_sign == -1
        assert cpi.z_score is None  # 歷史樣本 < 6 回傳 None

        # 檢驗 NFP
        nfp = next(r for r in readings if r.event_key == "NFP")
        assert nfp.actual == 220000.0
        assert nfp.forecast == 180000.0
        assert pytest.approx(nfp.raw_diff, 1e-4) == 40000.0
        assert nfp.growth_sign == 1

        assert mock_save.await_count == 2
