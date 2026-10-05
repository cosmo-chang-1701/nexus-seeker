"""單元測試：基本面共識與耳語提供者 (fundamental_providers.py)。"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from services.fundamental_providers import (
    FinnhubConsensusProvider,
    NullConsensusProvider,
    NullWhisperProvider,
)


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
            "date": "2026-10-20",
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
        patch(
            "services.market_data_service._core._get_client",
            return_value=mock_client,
        ),
        patch(
            "services.market_data_service._core._execute_api_call",
            side_effect=mock_exec_call,
        ),
    ):
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
        consensus = await provider.get_consensus("TSLA")
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
