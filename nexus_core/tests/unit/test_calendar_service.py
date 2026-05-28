import pytest
from unittest.mock import AsyncMock, patch
from services.calendar_service import CalendarService, EconomicEvent, EarningsEvent
from datetime import datetime, date


@pytest.mark.asyncio
async def test_get_high_impact_events():
    fixed_now = datetime(2026, 5, 12, 12, 0, 0)
    with patch("services.calendar_service.datetime") as mock_datetime:
        mock_datetime.now.side_effect = (
            lambda tz=None: fixed_now if tz is None else fixed_now.replace(tzinfo=tz)
        )
        mock_datetime.fromisoformat = datetime.fromisoformat
        mock_datetime.strptime = datetime.strptime
        mock_datetime.combine = datetime.combine
        mock_datetime.min = datetime.min

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

            service = CalendarService()
            events = await service.get_high_impact_events(days=7)
            service_reloaded = CalendarService()
            cached_events = await service_reloaded.get_high_impact_events(days=7)

    assert len(events) == 1
    assert isinstance(events[0], EconomicEvent)
    assert events[0].event == "CPI Report"
    assert events[0].impact == "high"
    assert len(cached_events) == 1
    assert mock_cal.call_count == 1


@pytest.mark.asyncio
async def test_get_symbol_earnings():
    fixed_now = datetime(2026, 5, 18, 12, 0, 0)
    with patch("services.calendar_service.datetime") as mock_datetime:
        mock_datetime.now.side_effect = (
            lambda tz=None: fixed_now if tz is None else fixed_now.replace(tzinfo=tz)
        )
        mock_datetime.fromisoformat = datetime.fromisoformat
        mock_datetime.combine = datetime.combine
        mock_datetime.min = datetime.min
        mock_datetime.strptime = datetime.strptime

        with patch(
            "services.market_data_service.get_earnings_calendar", new_callable=AsyncMock
        ) as mock_calendar:
            target_date = date(2026, 5, 20)
            mock_calendar.return_value = [{"date": target_date.isoformat()}]

            service = CalendarService()
            info = await service.get_symbol_earnings("AAPL")
            service_reloaded = CalendarService()
            cached_info = await service_reloaded.get_symbol_earnings("AAPL")

    assert isinstance(info, EarningsEvent)
    assert info.symbol == "AAPL"
    assert info.date == "2026-05-20"
    assert info.tte_hours == 36.0
    assert info.days_to_earnings == pytest.approx(1.5)
    assert isinstance(cached_info, EarningsEvent)
    assert cached_info.date == "2026-05-20"
    assert cached_info.days_to_earnings == pytest.approx(1.5)
    assert mock_calendar.await_count == 1


@pytest.mark.asyncio
async def test_get_symbol_earnings_timezone_robustness():
    from zoneinfo import ZoneInfo

    ny_tz = ZoneInfo("America/New_York")

    # Mock current time in NY as 2026-05-18 21:00:00
    fixed_now_ny = datetime(2026, 5, 18, 21, 0, 0, tzinfo=ny_tz)

    with patch("services.calendar_service.datetime") as mock_datetime:
        mock_datetime.now.side_effect = (
            lambda tz=None: fixed_now_ny if tz is None else fixed_now_ny.astimezone(tz)
        )
        mock_datetime.fromisoformat = datetime.fromisoformat
        mock_datetime.combine = datetime.combine
        mock_datetime.min = datetime.min
        mock_datetime.strptime = datetime.strptime

        with patch(
            "services.market_data_service.get_earnings_calendar", new_callable=AsyncMock
        ) as mock_calendar:
            mock_calendar.return_value = [{"date": "2026-05-19"}]

            service = CalendarService()
            info = await service.get_symbol_earnings("AAPL")

    assert isinstance(info, EarningsEvent)
    assert info.symbol == "AAPL"
    assert info.date == "2026-05-19"
    # 2026-05-19 00:00:00 NY time - 2026-05-18 21:00:00 NY time = 3.0 hours
    assert info.tte_hours == 3.0


@pytest.mark.asyncio
async def test_get_high_impact_events_filters_non_us():
    fixed_now = datetime(2026, 5, 12, 12, 0, 0)
    with patch("services.calendar_service.datetime") as mock_datetime:
        mock_datetime.now.side_effect = (
            lambda tz=None: fixed_now if tz is None else fixed_now.replace(tzinfo=tz)
        )
        mock_datetime.fromisoformat = datetime.fromisoformat
        mock_datetime.strptime = datetime.strptime
        mock_datetime.combine = datetime.combine
        mock_datetime.min = datetime.min

        with patch(
            "services.market_data_service.get_economic_calendar", autospec=True
        ) as mock_cal:
            mock_cal.return_value = [
                {
                    "event": "US CPI Report",
                    "impact": "high",
                    "time": "2026-05-15T12:30:00Z",
                    "country": "US",
                },
                {
                    "event": "CA Retail Sales",
                    "impact": "high",
                    "time": "2026-05-15T13:00:00Z",
                    "country": "CA",
                },
            ]

            service = CalendarService()
            events = await service.get_high_impact_events(days=7)

    # Asserts that the CA event was filtered out and only the US event remains
    assert len(events) == 1
    assert isinstance(events[0], EconomicEvent)
    assert events[0].event == "US CPI Report"
    assert events[0].country == "US"
