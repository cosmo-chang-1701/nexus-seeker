"""單元測試：基本面共識與耳語提供者 (fundamental_providers.py)。"""

from __future__ import annotations

from datetime import date, datetime
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch
from zoneinfo import ZoneInfo

import pytest
from services.fundamental_providers import (
    FinnhubConsensusProvider,
    NullConsensusProvider,
    NullWhisperProvider,
)

_CAL = "services.market_data_service.fundamentals.get_earnings_calendar"
_CLIENT = "services.market_data_service._core._get_client"
_EXEC = "services.market_data_service._core._execute_api_call"


def _et(year: int, month: int, day: int) -> datetime:
    return datetime(year, month, day, 12, 0, tzinfo=ZoneInfo("America/New_York"))


@pytest.mark.asyncio
async def test_null_providers() -> None:
    """測試 NullConsensusProvider 與 NullWhisperProvider 的安全性。"""
    c_null = NullConsensusProvider()
    assert await c_null.get_consensus("AAPL") is None
    assert await c_null.get_estimate_snapshots("AAPL") == []

    w_null = NullWhisperProvider()
    assert await w_null.get_whisper("AAPL") is None


@pytest.mark.asyncio
async def test_finnhub_consensus_provider_calendar_success() -> None:
    """測試 FinnhubConsensusProvider 透過 earnings calendar 成功解析數據。"""
    provider = FinnhubConsensusProvider()

    mock_calendar_entries = [
        {
            "date": "2026-07-20",
            "year": 2026,
            "quarter": 3,
            "epsActual": 1.55,
            "epsEstimate": 1.50,
            "revenueActual": 25000000000.0,
            "revenueEstimate": 24500000000.0,
            "hour": "amc",
            "symbol": "TSLA",
        }
    ]

    with patch(
        "services.market_data_service.fundamentals.get_earnings_calendar",
        new=AsyncMock(return_value=mock_calendar_entries),
    ):
        consensus = await provider.get_consensus("TSLA")
        assert consensus is not None
        assert consensus.symbol == "TSLA"
        assert consensus.fiscal_period == "2026-Q3"
        assert consensus.actual_eps == 1.55
        assert consensus.consensus_eps == 1.50
        assert consensus.actual_revenue == 25000000000.0
        assert consensus.session == "AMC"
        assert consensus.source == "finnhub"


@pytest.mark.asyncio
async def test_finnhub_consensus_provider_company_earnings_fallback() -> None:
    """測試 calendar 無資料時退回 company_earnings。"""
    provider = FinnhubConsensusProvider()

    mock_client = MagicMock()
    mock_client.company_earnings.return_value = [
        {
            "period": "2026-06-30",
            "year": 2026,
            "quarter": 2,
            "actual": 0.52,
            "estimate": 0.48,
            "symbol": "NVDA",
        }
    ]

    with (
        patch(
            "services.market_data_service.fundamentals.get_earnings_calendar",
            new=AsyncMock(return_value=[]),
        ),
        patch(
            "services.market_data_service._core._get_client",
            return_value=mock_client,
        ),
        patch(
            "services.market_data_service._core._execute_api_call",
            new=AsyncMock(return_value=mock_client.company_earnings.return_value),
        ),
    ):
        consensus = await provider.get_consensus("NVDA")
        assert consensus is not None
        assert consensus.fiscal_period == "2026-Q2"
        assert consensus.actual_eps == 0.52
        assert consensus.consensus_eps == 0.48


@pytest.mark.asyncio
async def test_finnhub_consensus_provider_estimate_snapshots() -> None:
    """測試抓取分析師 EPS 預估快照 (0q, +1q, 0y, +1y)。"""
    provider = FinnhubConsensusProvider()

    mock_q_data = {
        "data": [
            {
                "period": "2026-09-30",
                "epsAvg": 1.45,
                "epsHigh": 1.55,
                "epsLow": 1.35,
                "numberAnalysts": 28,
            },
            {
                "period": "2026-12-31",
                "epsAvg": 1.60,
                "epsHigh": 1.70,
                "epsLow": 1.50,
                "numberAnalysts": 26,
            },
        ]
    }
    mock_a_data = {
        "data": [
            {
                "period": "2026-12-31",
                "epsAvg": 5.80,
                "epsHigh": 6.10,
                "epsLow": 5.50,
                "numberAnalysts": 30,
            },
            {
                "period": "2027-12-31",
                "epsAvg": 6.80,
                "epsHigh": 7.20,
                "epsLow": 6.40,
                "numberAnalysts": 29,
            },
        ]
    }

    async def mock_exec_call(fn: Any, *args: Any, **kwargs: Any) -> Any:
        freq = kwargs.get("freq")
        if freq == "quarterly":
            return mock_q_data
        elif freq == "annual":
            return mock_a_data
        return {}

    mock_client = MagicMock()

    with (
        patch("services.fundamental_providers.datetime") as mock_dt,
        patch(
            "services.market_data_service._core._get_client",
            return_value=mock_client,
        ),
        patch(
            "services.market_data_service._core._execute_api_call",
            side_effect=mock_exec_call,
        ),
    ):
        mock_dt.now.return_value = _et(2026, 8, 15)
        snapshots = await provider.get_estimate_snapshots("AAPL")
        assert len(snapshots) == 4
        horizons = [s.horizon for s in snapshots]
        assert horizons == ["0q", "+1q", "0y", "+1y"]
        assert snapshots[0].eps_mean == 1.45
        assert snapshots[1].eps_mean == 1.60
        assert snapshots[2].eps_mean == 5.80
        assert snapshots[3].eps_mean == 6.80


@pytest.mark.asyncio
async def test_finnhub_consensus_provider_leap_year_and_malformed_data() -> None:
    """測試在閏日 (2/29) 執行與 API 回傳畸形字串時系統不崩潰。"""
    from datetime import datetime
    from zoneinfo import ZoneInfo

    provider = FinnhubConsensusProvider()
    leap_date = datetime(2024, 2, 29, 12, 0, tzinfo=ZoneInfo("America/New_York"))

    mock_entries = [
        {
            "date": "2024-02-28",
            "year": 2024,
            "quarter": 1,
            "epsActual": "N/A",  # 畸形字串
            "epsEstimate": "0.50",
            "revenueActual": None,
            "revenueEstimate": "5000000.0",
            "hour": "bmo",
        }
    ]

    with (
        patch("services.fundamental_providers.datetime") as mock_dt,
        patch(
            "services.market_data_service.fundamentals.get_earnings_calendar",
            new=AsyncMock(return_value=mock_entries),
        ),
    ):
        mock_dt.now.return_value = leap_date
        consensus = await provider.get_consensus("TSLA", "2024-Q1")
        assert consensus is not None
        assert consensus.actual_eps is None  # "N/A" 安全轉為 None
        assert consensus.consensus_eps == 0.50
        assert consensus.consensus_revenue == 5000000.0
        assert consensus.session == "BMO"


@pytest.mark.asyncio
async def test_finnhub_consensus_provider_company_earnings_skips_empty() -> None:
    """測試 company_earnings 遇到前項皆為 None 時正確跳過並提取有效項。"""
    provider = FinnhubConsensusProvider()
    mock_client = MagicMock()
    mock_client.company_earnings.return_value = [
        {"period": "2026-09-30", "actual": None, "estimate": None},  # 空項應跳過
        {
            "period": "2026-06-30",
            "year": 2026,
            "quarter": 2,
            "actual": 0.85,
            "estimate": 0.80,
        },
    ]

    with (
        patch(
            "services.market_data_service.fundamentals.get_earnings_calendar",
            new=AsyncMock(return_value=[]),
        ),
        patch(
            "services.market_data_service._core._get_client",
            return_value=mock_client,
        ),
        patch(
            "services.market_data_service._core._execute_api_call",
            new=AsyncMock(return_value=mock_client.company_earnings.return_value),
        ),
    ):
        consensus = await provider.get_consensus("GOOGL")
        assert consensus is not None
        assert consensus.fiscal_period == "2026-Q2"
        assert consensus.actual_eps == 0.85
        assert consensus.consensus_eps == 0.80


# ============================================================================
# 期別對齊（code review 修正）
# ============================================================================

_AAPL_CALENDAR: list[dict[str, Any]] = [
    {
        "date": "2025-10-30",
        "year": 2025,
        "quarter": 4,
        "epsActual": 1.85,
        "epsEstimate": 1.81,
        "revenueActual": 102.5e9,
        "revenueEstimate": 101.7e9,
        "hour": "amc",
    },
    {
        "date": "2026-01-29",
        "year": 2026,
        "quarter": 1,
        "epsActual": 2.84,
        "epsEstimate": 2.67,
        "revenueActual": 143.8e9,
        "revenueEstimate": 138.4e9,
        "hour": "amc",
    },
    {
        "date": "2026-04-30",
        "year": 2026,
        "quarter": 2,
        "epsActual": None,
        "epsEstimate": 1.99,
        "revenueActual": None,
        "revenueEstimate": 109.0e9,
        "hour": "amc",
    },
]


@pytest.mark.asyncio
async def test_get_consensus_latest_reported_not_oldest() -> None:
    """未指定財季時取「發布日 <= 今天且已有實際 EPS」的最後一筆，而非日曆中最舊一筆。"""
    provider = FinnhubConsensusProvider()
    with (
        patch("services.fundamental_providers.datetime") as mock_dt,
        patch(_CAL, new=AsyncMock(return_value=_AAPL_CALENDAR)),
    ):
        mock_dt.now.return_value = _et(2026, 2, 15)
        consensus = await provider.get_consensus("AAPL")
    assert consensus is not None
    assert consensus.fiscal_period == "2026-Q1"
    assert consensus.actual_eps == 2.84


@pytest.mark.asyncio
async def test_get_consensus_aligns_to_sec_acceptance_date() -> None:
    """以 SEC 受理日對齊財報日曆；實際值尚未更新時仍回傳該季共識（期別正確）。"""
    provider = FinnhubConsensusProvider()
    with (
        patch("services.fundamental_providers.datetime") as mock_dt,
        patch(_CAL, new=AsyncMock(return_value=_AAPL_CALENDAR)),
    ):
        mock_dt.now.return_value = _et(2026, 4, 30)
        consensus = await provider.get_consensus("AAPL", as_of=date(2026, 4, 30))
    assert consensus is not None
    assert consensus.fiscal_period == "2026-Q2"
    assert consensus.actual_eps is None
    assert consensus.consensus_eps == 1.99


@pytest.mark.asyncio
async def test_get_consensus_as_of_without_nearby_entry_returns_none() -> None:
    """受理日附近沒有財報條目（例如非財報 8-K 誤標）時不得挑選其他季度。"""
    provider = FinnhubConsensusProvider()
    with (
        patch(_CAL, new=AsyncMock(return_value=_AAPL_CALENDAR)),
        patch(_CLIENT, side_effect=RuntimeError("no client")),
    ):
        consensus = await provider.get_consensus("AAPL", as_of=date(2026, 3, 10))
    assert consensus is None


@pytest.mark.asyncio
async def test_get_consensus_skips_entries_without_fiscal_quarter() -> None:
    """缺 year / quarter 的條目不以發布日推算日曆季（非曆年制財年會錯置）。"""
    provider = FinnhubConsensusProvider()
    entries = [{"date": "2026-01-29", "epsActual": 2.84, "epsEstimate": 2.67}]
    with (
        patch(_CAL, new=AsyncMock(return_value=entries)),
        patch(_CLIENT, side_effect=RuntimeError("no client")),
    ):
        consensus = await provider.get_consensus("AAPL", as_of=date(2026, 1, 29))
    assert consensus is None


@pytest.mark.asyncio
async def test_get_consensus_company_earnings_aligned_by_report_lag() -> None:
    """company_earnings 備援以「財季期末日 < 受理日 <= 期末日 + 100 天」對齊。"""
    provider = FinnhubConsensusProvider()
    rows = [
        {
            "period": "2026-06-30",
            "year": 2026,
            "quarter": 3,
            "actual": 1.91,
            "estimate": 1.93,
        },
        {
            "period": "2026-03-31",
            "year": 2026,
            "quarter": 2,
            "actual": 2.01,
            "estimate": 1.99,
        },
    ]
    with (
        patch(_CAL, new=AsyncMock(return_value=[])),
        patch(_CLIENT, return_value=MagicMock()),
        patch(_EXEC, new=AsyncMock(return_value=rows)),
    ):
        consensus = await provider.get_consensus("AAPL", as_of=date(2026, 5, 1))
    assert consensus is not None
    assert consensus.fiscal_period == "2026-Q2"
    assert consensus.actual_eps == 2.01


@pytest.mark.asyncio
async def test_estimate_snapshots_horizon_starts_from_unfinished_period() -> None:
    """0q / 0y 以「期末日 >= 今天」為起點，已結束之期別不得標為 0q / 0y。"""
    provider = FinnhubConsensusProvider()
    q_rows = [
        {"period": "2026-03-31", "epsAvg": 1.10},
        {"period": "2026-06-30", "epsAvg": 1.20},
        {"period": "2026-09-30", "epsAvg": 1.30},
        {"period": "2026-12-31", "epsAvg": 1.40},
        {"period": "2027-03-31", "epsAvg": 1.50},
    ]
    a_rows = [
        {"period": "2025-12-31", "epsAvg": 4.0},
        {"period": "2026-12-31", "epsAvg": 5.0},
        {"period": "2027-12-31", "epsAvg": 6.0},
    ]

    async def exec_call(fn: Any, *args: Any, **kwargs: Any) -> Any:
        return {"data": q_rows if kwargs.get("freq") == "quarterly" else a_rows}

    with (
        patch("services.fundamental_providers.datetime") as mock_dt,
        patch(_CLIENT, return_value=MagicMock()),
        patch(_EXEC, side_effect=exec_call),
    ):
        mock_dt.now.return_value = _et(2026, 7, 15)
        snapshots = await provider.get_estimate_snapshots("AAPL")
    by_h = {s.horizon: s.eps_mean for s in snapshots}
    assert by_h == {"0q": 1.30, "+1q": 1.40, "0y": 5.0, "+1y": 6.0}


@pytest.mark.asyncio
async def test_estimate_snapshots_calendar_fallback_maps_current_fiscal_quarter() -> (
    None
):
    """eps-estimate 無權限（403）時以 company_earnings 錨定財季，再對齊日曆預估。

    實測資料（2026-10-07）：AAPL 最近已公布 FY2026 Q3（期末 2026-06-30）；日曆列出
    FY2026 Q4（10/29 發布，期末已過）、FY2027 Q1、FY2027 Q2。0q 應為 FY2027 Q1。
    """
    provider = FinnhubConsensusProvider()
    client = MagicMock()
    earnings_rows = [
        {
            "period": "2026-06-30",
            "year": 2026,
            "quarter": 3,
            "actual": 1.91,
            "estimate": 1.9271,
        },
        {
            "period": "2026-03-31",
            "year": 2026,
            "quarter": 2,
            "actual": 2.01,
            "estimate": 1.9884,
        },
    ]
    calendar_rows = [
        {"date": "2026-10-29", "year": 2026, "quarter": 4, "epsEstimate": 2.0214},
        {"date": "2027-01-27", "year": 2027, "quarter": 1, "epsEstimate": 2.9512},
        {"date": "2027-04-28", "year": 2027, "quarter": 2, "epsEstimate": 2.2866},
    ]

    async def exec_call(fn: Any, *args: Any, **kwargs: Any) -> Any:
        if fn is client.company_eps_estimates:
            raise RuntimeError("FinnhubAPIException(status_code: 403)")
        if fn is client.company_earnings:
            return earnings_rows
        raise AssertionError("unexpected call")

    with (
        patch("services.fundamental_providers.datetime") as mock_dt,
        patch(_CLIENT, return_value=client),
        patch(_EXEC, side_effect=exec_call),
        patch(_CAL, new=AsyncMock(return_value=calendar_rows)),
    ):
        mock_dt.now.return_value = _et(2026, 10, 7)
        snapshots = await provider.get_estimate_snapshots("AAPL")

    by_h = {s.horizon: s.eps_mean for s in snapshots}
    assert by_h == {"0q": 2.9512, "+1q": 2.2866}
    assert all(s.source == "finnhub_calendar" for s in snapshots)


@pytest.mark.asyncio
async def test_estimate_snapshots_calendar_fallback_without_anchor_writes_nothing() -> (
    None
):
    """無已公布財季可錨定時不寫入快照，避免把「已結束待公布」的財季標成 0q。"""
    provider = FinnhubConsensusProvider()
    with (
        patch(_CLIENT, return_value=MagicMock()),
        patch(_EXEC, new=AsyncMock(side_effect=RuntimeError("403"))),
        patch(
            _CAL,
            new=AsyncMock(
                return_value=[
                    {
                        "date": "2026-10-29",
                        "year": 2026,
                        "quarter": 4,
                        "epsEstimate": 2.02,
                    }
                ]
            ),
        ),
    ):
        assert await provider.get_estimate_snapshots("AAPL") == []
