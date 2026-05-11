import pytest
from unittest.mock import AsyncMock, patch
from services.calendar_service import CalendarService
from datetime import datetime, timedelta


@pytest.mark.asyncio
async def test_get_high_impact_events():
    service = CalendarService()

    # Mock market_data_service.get_economic_calendar
    with patch(
        "services.market_data_service.get_economic_calendar", new_callable=AsyncMock
    ) as mock_cal:
        mock_cal.return_value = [
            {
                "event": "CPI Report",
                "impact": "high",
                "time": "2026-05-15T12:30:00Z",
                "country": "US",
            },
            {
                "event": "Unimportant Data",
                "impact": "low",
                "time": "2026-05-15T13:00:00Z",
                "country": "US",
            },
        ]

        events = await service.get_high_impact_events(days=7)
        assert len(events) == 1
        assert events[0]["event"] == "CPI Report"
        assert events[0]["impact"] == "high"


@pytest.mark.asyncio
async def test_get_symbol_earnings():
    service = CalendarService()

    with patch(
        "market_analysis.data.get_next_earnings_date", new_callable=AsyncMock
    ) as mock_date:
        next_dt = datetime.now() + timedelta(days=2)
        mock_date.return_value = next_dt

        info = await service.get_symbol_earnings("AAPL")
        assert info["symbol"] == "AAPL"
        assert 47.0 < info["tte_hours"] < 49.0
