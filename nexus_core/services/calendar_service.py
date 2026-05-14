import asyncio
import logging
from datetime import datetime, timedelta, date
from typing import List, Optional, Union
from pydantic import BaseModel, field_validator
from services import market_data_service
from services.market_data_service import BoundedCache

logger = logging.getLogger(__name__)


# ==========================================
# 📊 Pydantic Models for Type Safety
# ==========================================


class CalendarEvent(BaseModel):
    """Base model for all calendar events."""

    type: str
    tte_hours: float

    @property
    def is_imminent(self) -> bool:
        return 0 < self.tte_hours <= 24


class EconomicEvent(CalendarEvent):
    """Model for macro economic events (CPI, FOMC, etc)."""

    type: str = "ECONOMIC"
    event: str
    time: str  # ISO format string
    impact: str
    country: str = "US"

    @field_validator("time")
    @classmethod
    def validate_time(cls, v: str) -> str:
        # Basic validation to ensure it's a parseable timestamp
        try:
            datetime.fromisoformat(v.replace("Z", "+00:00"))
        except ValueError:
            raise ValueError(f"Invalid timestamp format: {v}")
        return v


class EarningsEvent(CalendarEvent):
    """Model for equity earnings events."""

    type: str = "EARNINGS"
    symbol: str
    date: str  # YYYY-MM-DD

    @field_validator("date")
    @classmethod
    def validate_date(cls, v: str) -> str:
        try:
            datetime.strptime(v, "%Y-%m-%d")
        except ValueError:
            raise ValueError(f"Invalid date format: {v}")
        return v


class CalendarService:
    """
    Service for monitoring major economic events (CPI, FOMC) and equity earnings.
    Implements LRU Bounded Cache for 1GB RAM optimization and Pydantic for type safety.
    """

    def __init__(self):
        # LRU Bounded Cache with 500 entries
        self._economic_cache = BoundedCache(max_size=500)
        self._earnings_cache = BoundedCache(max_size=500)
        self._high_impact_keywords = [
            "CPI",
            "FOMC",
            "Fed Interest Rate",
            "Nonfarm Payrolls",
            "Employment Situation",
        ]

    async def get_high_impact_events(self, days: int = 7) -> List[EconomicEvent]:
        """
        Fetch high-impact economic events from Finnhub within a rolling window.
        """
        now = datetime.now()
        start_date = now.strftime("%Y-%m-%d")
        end_date = (now + timedelta(days=days)).strftime("%Y-%m-%d")

        cache_key = f"economic_{start_date}_{end_date}"
        if cache_key in self._economic_cache:
            return self._economic_cache[cache_key]

        try:
            raw_events = await market_data_service.get_economic_calendar(
                start_date, end_date
            )

            high_impact = []
            for event in raw_events:
                impact = event.get("impact", "low")
                name = event.get("event", "")

                is_high = impact.lower() == "high" or any(
                    kw in name for kw in self._high_impact_keywords
                )

                if is_high:
                    event_time_str = event.get("time", "")
                    if event_time_str:
                        event_dt = datetime.fromisoformat(
                            event_time_str.replace("Z", "+00:00")
                        )
                        # Ensure we compare timezone-aware with timezone-aware or naive with naive
                        # event_dt from isoformat is aware if it has +00:00
                        now_aware = datetime.now(event_dt.tzinfo)
                        tte_hours = (event_dt - now_aware).total_seconds() / 3600
                    else:
                        tte_hours = 0.0

                    try:
                        ev = EconomicEvent(
                            event=name,
                            time=event_time_str,
                            impact=impact,
                            country=event.get("country", "US"),
                            tte_hours=round(tte_hours, 1),
                        )
                        high_impact.append(ev)
                    except Exception as ve:
                        logger.warning(f"Skipping malformed economic event: {ve}")

            self._economic_cache[cache_key] = high_impact
            return high_impact

        except Exception as e:
            logger.error(f"Failed to fetch economic calendar: {e}")
            return []

    async def get_symbol_earnings(self, symbol: str) -> Optional[EarningsEvent]:
        """
        Get the next earnings date for a specific symbol.
        """
        if symbol in self._earnings_cache:
            return self._earnings_cache[symbol]

        try:
            from market_analysis.data import get_next_earnings_date

            next_date = await get_next_earnings_date(symbol)

            if next_date:
                if isinstance(next_date, str):
                    next_dt = datetime.strptime(next_date, "%Y-%m-%d")
                    date_str = next_date
                elif isinstance(next_date, date):
                    next_dt = datetime.combine(next_date, datetime.min.time())
                    date_str = next_date.strftime("%Y-%m-%d")
                else:
                    next_dt = next_date
                    date_str = next_dt.strftime("%Y-%m-%d")

                now = datetime.now()
                tte_hours = (next_dt - now).total_seconds() / 3600

                try:
                    earnings_info = EarningsEvent(
                        symbol=symbol,
                        date=date_str,
                        tte_hours=round(tte_hours, 1),
                    )
                    self._earnings_cache[symbol] = earnings_info
                    return earnings_info
                except Exception as ve:
                    logger.warning(
                        f"Skipping malformed earnings event for {symbol}: {ve}"
                    )

        except Exception as e:
            logger.error(f"Failed to fetch earnings for {symbol}: {e}")

        return None

    async def get_portfolio_events(
        self, user_id: int, days: int = 7
    ) -> List[Union[EconomicEvent, EarningsEvent]]:
        """
        Get all high-impact events and earnings affecting a user's holdings.
        """
        from database.holdings import get_user_holdings

        holdings = await asyncio.to_thread(get_user_holdings, user_id)
        symbols = [h["symbol"] for h in holdings]

        events_task = self.get_high_impact_events(days)
        earnings_tasks = [self.get_symbol_earnings(s) for s in symbols]

        results = await asyncio.gather(events_task, *earnings_tasks)

        from typing import cast
        economic_events = cast(List[EconomicEvent], results[0])
        earnings_events = [cast(EarningsEvent, e) for e in results[1:] if e is not None]

        all_events: List[Union[EconomicEvent, EarningsEvent]] = []
        all_events.extend(economic_events)
        all_events.extend(earnings_events)

        combined = sorted(all_events, key=lambda x: x.tte_hours)
        return combined


# Singleton instance
calendar_service = CalendarService()
