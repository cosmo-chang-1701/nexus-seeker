import pytest
from unittest.mock import AsyncMock, patch
from services.calendar_service import CalendarService, EconomicEvent, EarningsEvent
from datetime import datetime, date


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

    fixed_now = datetime(2026, 5, 18, 12, 0, 0)
    with patch("services.calendar_service.datetime") as mock_datetime:
        mock_datetime.now.return_value = fixed_now
        mock_datetime.combine = datetime.combine
        mock_datetime.min = datetime.min
        mock_datetime.strptime = datetime.strptime

        with patch(
            "market_analysis.data.get_next_earnings_date", new_callable=AsyncMock
        ) as mock_date:
            # get_next_earnings_date returns a date object
            # Set it to 2 days after our fixed_now
            target_date = date(2026, 5, 20)
            mock_date.return_value = target_date

            info = await service.get_symbol_earnings("AAPL")
            assert isinstance(info, EarningsEvent)
            assert info.symbol == "AAPL"
            assert info.date == "2026-05-20"
            # (May 20, 00:00 - May 18, 12:00) = 36 hours
            assert info.tte_hours == 36.0
