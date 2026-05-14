import pytest
from unittest.mock import AsyncMock, patch
from services.calendar_service import CalendarService, EconomicEvent, EarningsEvent
from datetime import datetime, timedelta


@pytest.mark.asyncio
async def test_get_high_impact_events():
    service = CalendarService()

    # Mock market_data_service.get_economic_calendar
    with patch(
        "services.market_data_service.get_economic_calendar", autospec=True
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
        assert isinstance(events[0], EconomicEvent)
        assert events[0].event == "CPI Report"
        assert events[0].impact == "high"


@pytest.mark.asyncio
async def test_get_symbol_earnings():
    service = CalendarService()

    with patch(
        "market_analysis.data.get_next_earnings_date", new_callable=AsyncMock
    ) as mock_date:
        # get_next_earnings_date returns a date object
        next_dt = datetime.now() + timedelta(days=2)
        mock_date.return_value = next_dt.date()

        info = await service.get_symbol_earnings("AAPL")
        assert isinstance(info, EarningsEvent)
        assert info.symbol == "AAPL"
        # tte_hours depends on current time, but should be around 2 days (48h) +/- 12h
        assert 30.0 < info.tte_hours < 60.0
