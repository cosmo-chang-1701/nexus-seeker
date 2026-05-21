from datetime import datetime
from unittest.mock import AsyncMock, patch

import pytest

import database
from services.calendar_service import CalendarService, calendar_service
from services.trading_service import TradingService


@pytest.mark.asyncio
async def test_calendar_service_reuses_sqlite_macro_cache_across_instances(db_conn):
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
            "services.market_data_service.get_economic_calendar", new_callable=AsyncMock
        ) as mock_calendar:
            mock_calendar.return_value = [
                {
                    "event": "FOMC Rate Decision",
                    "impact": "high",
                    "time": "2026-05-15T18:00:00Z",
                    "country": "US",
                }
            ]

            first_service = CalendarService()
            first_events = await first_service.get_high_impact_events(days=7)

            second_service = CalendarService()
            second_events = await second_service.get_high_impact_events(days=7)

    assert len(first_events) == 1
    assert len(second_events) == 1
    assert first_events[0].event == "FOMC Rate Decision"
    assert second_events[0].event == "FOMC Rate Decision"
    assert mock_calendar.await_count == 1


@pytest.mark.asyncio
async def test_pre_market_alerts_use_sqlite_earnings_cache(db_conn):
    uid = 1001
    database.add_watchlist_symbol(uid, "AAPL")

    fixed_now = datetime(2026, 5, 18, 12, 0, 0)
    calendar_service._earnings_cache.clear()

    with patch("services.calendar_service.datetime") as calendar_datetime, patch(
        "services.trading_service.datetime"
    ) as trading_datetime:
        calendar_datetime.now.side_effect = (
            lambda tz=None: fixed_now if tz is None else fixed_now.replace(tzinfo=tz)
        )
        calendar_datetime.fromisoformat = datetime.fromisoformat
        calendar_datetime.strptime = datetime.strptime
        calendar_datetime.combine = datetime.combine
        calendar_datetime.min = datetime.min

        trading_datetime.now.return_value = fixed_now
        trading_datetime.strptime = datetime.strptime

        with patch(
            "services.market_data_service.get_earnings_calendar", new_callable=AsyncMock
        ) as mock_calendar:
            mock_calendar.return_value = [{"date": "2026-05-20"}]

            service = TradingService(bot=None)
            first_results = await service.get_pre_market_alerts_data(warning_days=3)

            calendar_service._earnings_cache.clear()
            second_results = await service.get_pre_market_alerts_data(warning_days=3)

    first_alerts = first_results[uid]["alerts"]
    second_alerts = second_results[uid]["alerts"]

    assert len(first_alerts) == 1
    assert len(second_alerts) == 1
    assert first_alerts[0]["symbol"] == "AAPL"
    assert second_alerts[0]["symbol"] == "AAPL"
    assert mock_calendar.await_count == 1
