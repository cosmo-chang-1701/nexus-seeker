import asyncio
import logging
from datetime import datetime, timedelta
from typing import List, Dict, Any, Optional
from services import market_data_service
from services.market_data_service import BoundedCache

logger = logging.getLogger(__name__)


class CalendarService:
    """
    Service for monitoring major economic events (CPI, FOMC) and equity earnings.
    Implements LRU Bounded Cache for 1GB RAM optimization.
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

    async def get_high_impact_events(self, days: int = 7) -> List[Dict[str, Any]]:
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
            # We'll need to add get_economic_calendar to market_data_service
            raw_events = await market_data_service.get_economic_calendar(
                start_date, end_date
            )

            high_impact = []
            for event in raw_events:
                # Filter for high impact based on impact field or keywords
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
                        tte_hours = (
                            event_dt.replace(tzinfo=None) - now
                        ).total_seconds() / 3600
                    else:
                        tte_hours = 0.0

                    high_impact.append(
                        {
                            "type": "ECONOMIC",
                            "event": name,
                            "time": event_time_str,
                            "impact": impact,
                            "country": event.get("country", "US"),
                            "tte_hours": round(tte_hours, 1),
                        }
                    )

            self._economic_cache[cache_key] = high_impact
            return high_impact

        except Exception as e:
            logger.error(f"Failed to fetch economic calendar: {e}")
            return []

    async def get_symbol_earnings(self, symbol: str) -> Optional[Dict[str, Any]]:
        """
        Get the next earnings date for a specific symbol.
        """
        if symbol in self._earnings_cache:
            return self._earnings_cache[symbol]

        try:
            # Use market_data_service to get next earnings
            from market_analysis.data import get_next_earnings_date

            next_date = await get_next_earnings_date(symbol)

            if next_date:
                if isinstance(next_date, str):
                    next_dt = datetime.strptime(next_date, "%Y-%m-%d")
                else:
                    next_dt = next_date

                now = datetime.now()
                tte_hours = (next_dt - now).total_seconds() / 3600

                earnings_info = {
                    "type": "EARNINGS",
                    "symbol": symbol,
                    "date": next_dt.strftime("%Y-%m-%d"),
                    "tte_hours": round(tte_hours, 1),
                }
                self._earnings_cache[symbol] = earnings_info
                return earnings_info

        except Exception as e:
            logger.error(f"Failed to fetch earnings for {symbol}: {e}")

        return None

    async def get_portfolio_events(
        self, user_id: int, days: int = 7
    ) -> List[Dict[str, Any]]:
        """
        Get all high-impact events and earnings affecting a user's holdings.
        """
        from database.holdings import get_user_holdings

        # holdings is a sync call in DB layer, but should be used with to_thread if frequent
        holdings = await asyncio.to_thread(get_user_holdings, user_id)
        symbols = [h["symbol"] for h in holdings]

        events_task = self.get_high_impact_events(days)
        earnings_tasks = [self.get_symbol_earnings(s) for s in symbols]

        results = await asyncio.gather(events_task, *earnings_tasks)

        economic_events = results[0]
        earnings_events = [e for e in results[1:] if e is not None]

        return sorted(economic_events + earnings_events, key=lambda x: x["tte_hours"])


# Singleton instance
calendar_service = CalendarService()
