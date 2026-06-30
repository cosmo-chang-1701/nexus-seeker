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

        from unittest.mock import MagicMock

        with patch("httpx.AsyncClient.get", new_callable=AsyncMock) as mock_get:
            mock_response = MagicMock()
            mock_response.status_code = 200
            mock_response.json.return_value = [
                {
                    "event_name": "CPI Report",
                    "date": "2026-05-15",
                    "time": "12:30",
                }
            ]
            mock_get.return_value = mock_response

            service = CalendarService()
            events = await service.get_high_impact_events(days=7)
            service_reloaded = CalendarService()
            cached_events = await service_reloaded.get_high_impact_events(days=7)

    assert len(events) == 1
    assert isinstance(events[0], EconomicEvent)
    assert events[0].event == "CPI Report"
    assert events[0].impact == "high"
    assert len(cached_events) == 1
    assert mock_get.call_count == 1


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
async def test_calendar_service_cold_start_policy():
    """Test that CalendarService cold-start cache-first policy bypasses API calls if cache exists."""
    fixed_now = datetime(2026, 5, 12, 12, 0, 0)
    with patch("services.calendar_service.datetime") as mock_datetime:
        mock_datetime.now.side_effect = (
            lambda tz=None: fixed_now if tz is None else fixed_now.replace(tzinfo=tz)
        )
        mock_datetime.fromisoformat = datetime.fromisoformat
        mock_datetime.strptime = datetime.strptime
        mock_datetime.combine = datetime.combine
        mock_datetime.min = datetime.min

        # 1. Test macro events bypass
        with patch(
            "services.calendar_service.get_macro_month_status"
        ) as mock_status, patch(
            "services.calendar_service.get_macro_events_between"
        ) as mock_events_between, patch(
            "httpx.AsyncClient.get", new_callable=AsyncMock
        ) as mock_get:
            mock_status.return_value = {
                "month_key": "2026-05",
                "checked_at": "2026-05-01 00:00:00",
            }
            mock_events_between.return_value = [
                {
                    "event": "FOMC",
                    "event_time": "2026-05-15T12:30:00Z",
                    "impact": "high",
                    "country": "US",
                }
            ]

            service = CalendarService()
            assert service._cold_start_complete is False

            events = await service.get_high_impact_events(days=3)
            assert service._cold_start_complete is True

            mock_get.assert_not_called()
            assert len(events) == 1
            assert events[0].event == "FOMC"

        # 2. Test earnings bypass
        with patch(
            "services.calendar_service.get_cached_earnings"
        ) as mock_get_earnings, patch(
            "services.market_data_service.get_earnings_calendar"
        ) as mock_earnings_finnhub:
            mock_get_earnings.return_value = {
                "symbol": "AAPL",
                "earnings_date": "2026-05-20",
                "checked_at": "2026-05-01 00:00:00",
            }

            service = CalendarService()
            assert service._cold_start_complete is False

            earnings = await service.get_symbol_earnings("AAPL")
            mock_earnings_finnhub.assert_not_called()
            assert earnings is not None
            assert earnings.date == "2026-05-20"


@pytest.mark.asyncio
async def test_calendar_service_swr_fallback(db_conn):
    """Test SWR fallback: when scraper fails, keep existing cache and mark fallback=True."""
    fixed_now = datetime(2026, 5, 12, 12, 0, 0)
    month_key = "2026-05"

    # Pre-populate database cache
    from database.calendar_cache import replace_macro_month_events

    pre_events = [
        {
            "event": "Cached FOMC",
            "time": "2026-05-15T18:00:00Z",
            "impact": "high",
            "country": "US",
        }
    ]
    replace_macro_month_events(month_key, pre_events)

    # Manually make the cache stale relative to fixed_now
    import sqlite3
    import config

    conn = sqlite3.connect(config.DB_NAME)
    cursor = conn.cursor()
    cursor.execute(
        "UPDATE economic_calendar_month_cache SET checked_at = '2026-05-01 00:00:00' WHERE month_key = ?",
        (month_key,),
    )
    conn.commit()
    conn.close()

    with patch("services.calendar_service.datetime") as mock_datetime:
        mock_datetime.now.side_effect = (
            lambda tz=None: fixed_now if tz is None else fixed_now.replace(tzinfo=tz)
        )
        mock_datetime.fromisoformat = datetime.fromisoformat
        mock_datetime.strptime = datetime.strptime
        mock_datetime.combine = datetime.combine
        mock_datetime.min = datetime.min

        # Mock API request to fail (e.g. status code 500)
        from unittest.mock import MagicMock

        with patch("httpx.AsyncClient.get", new_callable=AsyncMock) as mock_get:
            mock_response = MagicMock()
            mock_response.status_code = 500
            mock_get.return_value = mock_response

            service = CalendarService()
            service._cold_start_complete = True
            # Force cache refresh so it tries to fetch from API
            events = await service.get_high_impact_events(days=7)

    # 1. Assert existing cache was NOT wiped out
    assert len(events) == 1
    assert events[0].event == "Cached FOMC"

    # 2. Assert is_fallback attribute is True
    assert getattr(events, "is_fallback", False) is True


@pytest.mark.asyncio
async def test_calendar_service_swr_no_cache_failure(db_conn):
    """Test SWR fallback when no cache is available: returns empty list with fallback=False."""
    fixed_now = datetime(2026, 5, 12, 12, 0, 0)
    month_key = "2026-05"

    # Clear cache state completely
    import sqlite3
    import config

    conn = sqlite3.connect(config.DB_NAME)
    cursor = conn.cursor()
    cursor.execute(
        "DELETE FROM economic_calendar_month_cache WHERE month_key = ?", (month_key,)
    )
    cursor.execute(
        "DELETE FROM economic_calendar_events WHERE month_key = ?", (month_key,)
    )
    conn.commit()
    conn.close()

    with patch("services.calendar_service.datetime") as mock_datetime:
        mock_datetime.now.side_effect = (
            lambda tz=None: fixed_now if tz is None else fixed_now.replace(tzinfo=tz)
        )
        mock_datetime.fromisoformat = datetime.fromisoformat
        mock_datetime.strptime = datetime.strptime
        mock_datetime.combine = datetime.combine
        mock_datetime.min = datetime.min

        from unittest.mock import MagicMock

        with patch("httpx.AsyncClient.get", new_callable=AsyncMock) as mock_get:
            mock_response = MagicMock()
            mock_response.status_code = 500
            mock_get.return_value = mock_response

            service = CalendarService()
            # Must force fresh checking behavior
            service._cold_start_complete = True
            events = await service.get_high_impact_events(days=7)

    # Assert empty list and fallback is False
    assert len(events) == 0
    assert getattr(events, "is_fallback", False) is False


@pytest.mark.asyncio
async def test_calendar_embed_with_fallback():
    """Test build_calendar_embed correctly displays fallback suffix in title."""
    from services.calendar_service import EconomicEventList, EconomicEvent
    from cogs.embed_builders import build_calendar_embed

    macro_events = EconomicEventList(
        [
            EconomicEvent(
                type="ECONOMIC",
                event="FOMC",
                time="2026-05-15T18:00:00Z",
                impact="high",
                country="US",
                tte_hours=24.0,
            )
        ]
    )
    macro_events.is_fallback = True

    embed = build_calendar_embed(
        macro_events=macro_events,
        earnings_events=[],
        fedwatch_prob=0.75,
    )

    assert "總經數據暫時無法獲取，正使用本地歷史快取" in embed.title
