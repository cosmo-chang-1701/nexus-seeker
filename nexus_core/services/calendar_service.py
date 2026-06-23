import asyncio
import logging
from datetime import date, datetime, timedelta
from typing import List, Optional, Union
from zoneinfo import ZoneInfo

from pydantic import BaseModel, computed_field, field_validator
from database.calendar_cache import (
    get_cached_earnings,
    get_macro_events_between,
    get_macro_month_status,
    replace_macro_month_events,
    save_earnings_cache,
)
from services import market_data_service
from services.market_data_service import BoundedCache

ny_tz = ZoneInfo("America/New_York")

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
    consensus_value: Optional[str] = None
    fedwatch_probability: Optional[float] = None

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

    @computed_field  # type: ignore[misc]
    @property
    def days_to_earnings(self) -> float:
        return round(self.tte_hours / 24.0, 2)


class CalendarService:
    """
    Service for monitoring major economic events (CPI, FOMC) and equity earnings.
    Implements LRU Bounded Cache for 1GB RAM optimization and Pydantic for type safety.
    """

    def __init__(self):
        # LRU Bounded Cache with 500 entries
        self._economic_cache = BoundedCache(max_size=500)
        self._earnings_cache = BoundedCache(max_size=500)
        self._macro_cache_hours = 24
        self._earnings_cache_hours = 24
        self._cold_start_complete = False
        self._high_impact_keywords = [
            "CPI",
            "FOMC",
            "Fed Interest Rate",
            "Nonfarm Payrolls",
            "Employment Situation",
        ]

    def _is_timestamp_fresh(self, raw_ts: Optional[str], max_age_hours: int) -> bool:
        if not raw_ts:
            return False
        try:
            checked_at = datetime.fromisoformat(raw_ts.replace(" ", "T"))
        except ValueError:
            return False
        return checked_at >= datetime.now() - timedelta(hours=max_age_hours)

    def _iter_month_keys(self, start_date: date, end_date: date) -> list[str]:
        cursor = date(start_date.year, start_date.month, 1)
        month_keys: list[str] = []
        while cursor <= end_date:
            month_keys.append(cursor.strftime("%Y-%m"))
            if cursor.month == 12:
                cursor = date(cursor.year + 1, 1, 1)
            else:
                cursor = date(cursor.year, cursor.month + 1, 1)
        return month_keys

    def _month_bounds(self, month_key: str) -> tuple[str, str]:
        month_start = datetime.strptime(f"{month_key}-01", "%Y-%m-%d").date()
        if month_start.month == 12:
            next_month = date(month_start.year + 1, 1, 1)
        else:
            next_month = date(month_start.year, month_start.month + 1, 1)
        month_end = next_month - timedelta(days=1)
        return month_start.isoformat(), month_end.isoformat()

    def _extract_next_earnings_date(
        self, entries: list[dict[str, object]] | None
    ) -> Optional[date]:
        if not entries:
            return None

        today = datetime.now(ny_tz).date()
        parsed_dates: list[date] = []
        for entry in entries:
            raw_date = entry.get("date")
            if not raw_date or not isinstance(raw_date, str):
                continue
            try:
                parsed_dates.append(date.fromisoformat(raw_date))
            except ValueError:
                continue

        if not parsed_dates:
            return None

        for item in parsed_dates:
            if item >= today:
                return item
        return parsed_dates[-1]

    async def _ensure_macro_month_cached(
        self, month_key: str, force_fresh: bool = True
    ) -> None:
        status = get_macro_month_status(month_key)
        if status:
            if not force_fresh or self._is_timestamp_fresh(
                status.get("checked_at"), self._macro_cache_hours
            ):
                return

        start_date, end_date = self._month_bounds(month_key)
        raw_events = await market_data_service.get_economic_calendar(
            start_date, end_date
        )

        high_impact: list[dict[str, str]] = []
        for event in raw_events:
            raw_country = event.get("country")
            if not raw_country or not isinstance(raw_country, str):
                continue
            country = raw_country.strip().upper()
            if country != "US":
                continue

            impact = str(event.get("impact", "low"))
            name = str(event.get("event", ""))
            is_high = impact.lower() == "high" or any(
                kw in name for kw in self._high_impact_keywords
            )
            if not is_high:
                continue

            event_time_str = str(event.get("time", ""))
            if not event_time_str:
                continue

            high_impact.append(
                {
                    "event": name,
                    "time": event_time_str,
                    "impact": impact,
                    "country": country,
                }
            )

        replace_macro_month_events(month_key, high_impact)

    async def prefetch_monthly_macro_cache(
        self, reference: Optional[datetime] = None, months_ahead: int = 0
    ) -> None:
        now = reference or datetime.now()
        target = date(now.year, now.month, 1)
        month_keys = [target.strftime("%Y-%m")]
        cursor = target
        for _ in range(months_ahead):
            if cursor.month == 12:
                cursor = date(cursor.year + 1, 1, 1)
            else:
                cursor = date(cursor.year, cursor.month + 1, 1)
            month_keys.append(cursor.strftime("%Y-%m"))

        await asyncio.gather(
            *(self._ensure_macro_month_cached(key) for key in month_keys)
        )

    async def get_high_impact_events(self, days: int = 7) -> List[EconomicEvent]:
        """
        Fetch high-impact economic events from Finnhub within a rolling window.
        """
        now = datetime.now()
        start_day = now.date()
        end_day = (now + timedelta(days=days)).date()
        start_date = start_day.strftime("%Y-%m-%d")
        end_date = end_day.strftime("%Y-%m-%d")

        cache_key = f"economic_{start_date}_{end_date}"
        if cache_key in self._economic_cache:
            return self._economic_cache[cache_key]

        try:
            month_keys = self._iter_month_keys(start_day, end_day)

            tasks = []
            for key in month_keys:
                force_fresh = True
                if not self._cold_start_complete:
                    if get_macro_month_status(key) is not None:
                        force_fresh = False
                tasks.append(
                    self._ensure_macro_month_cached(key, force_fresh=force_fresh)
                )

            if not self._cold_start_complete:
                self._cold_start_complete = True

            await asyncio.gather(*tasks)
            raw_events = get_macro_events_between(start_date, end_date)

            high_impact = []
            for event in raw_events:
                event_time_str = str(event.get("event_time", ""))
                if not event_time_str:
                    continue
                event_dt = datetime.fromisoformat(event_time_str.replace("Z", "+00:00"))
                now_aware = datetime.now(event_dt.tzinfo)
                tte_hours = (event_dt - now_aware).total_seconds() / 3600

                try:
                    ev = EconomicEvent(
                        event=str(event.get("event", "")),
                        time=event_time_str,
                        impact=str(event.get("impact", "high")),
                        country=str(event.get("country", "US")),
                        tte_hours=round(tte_hours, 1),
                        consensus_value=event.get("consensus_value"),
                        fedwatch_probability=event.get("fedwatch_probability"),
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
        symbol = symbol.upper()
        if symbol in self._earnings_cache:
            return self._earnings_cache[symbol]

        try:
            cached = get_cached_earnings(symbol)
            today = datetime.now(ny_tz).date()
            if cached and (
                self._is_timestamp_fresh(
                    cached.get("checked_at"), self._earnings_cache_hours
                )
                or not self._cold_start_complete
            ):
                cached_date = cached.get("earnings_date")
                if not cached_date:
                    self._earnings_cache[symbol] = None
                    return None
                try:
                    parsed_cached = datetime.strptime(cached_date, "%Y-%m-%d").date()
                except ValueError:
                    parsed_cached = None

                if parsed_cached is not None and parsed_cached >= today:
                    next_dt = datetime.combine(
                        parsed_cached, datetime.min.time()
                    ).replace(tzinfo=ny_tz)
                    tte_hours = (next_dt - datetime.now(ny_tz)).total_seconds() / 3600
                    earnings_info = EarningsEvent(
                        symbol=symbol,
                        date=parsed_cached.strftime("%Y-%m-%d"),
                        tte_hours=round(tte_hours, 1),
                    )
                    self._earnings_cache[symbol] = earnings_info
                    return earnings_info

            raw_entries = await market_data_service.get_earnings_calendar(symbol)
            next_date = self._extract_next_earnings_date(raw_entries)
            save_earnings_cache(
                symbol, next_date.strftime("%Y-%m-%d") if next_date else None
            )

            if next_date is not None:
                next_dt = datetime.combine(next_date, datetime.min.time()).replace(
                    tzinfo=ny_tz
                )
                now = datetime.now(ny_tz)
                tte_hours = (next_dt - now).total_seconds() / 3600

                try:
                    earnings_info = EarningsEvent(
                        symbol=symbol,
                        date=next_date.strftime("%Y-%m-%d"),
                        tte_hours=round(tte_hours, 1),
                    )
                    self._earnings_cache[symbol] = earnings_info
                    return earnings_info
                except Exception as ve:
                    logger.warning(
                        f"Skipping malformed earnings event for {symbol}: {ve}"
                    )
            else:
                self._earnings_cache[symbol] = None

        except Exception as e:
            logger.error(f"Failed to fetch earnings for {symbol}: {e}")

        return None

    async def get_symbol_earnings_batch(
        self, symbols: List[str]
    ) -> dict[str, Optional[EarningsEvent]]:
        unique_symbols = sorted({symbol.upper() for symbol in symbols if symbol})
        results = await asyncio.gather(
            *(self.get_symbol_earnings(symbol) for symbol in unique_symbols)
        )
        return dict(zip(unique_symbols, results))

    async def get_next_high_impact_event(
        self, *, days: int = 7, max_tte_hours: Optional[float] = None
    ) -> Optional[EconomicEvent]:
        events = await self.get_high_impact_events(days=days)
        for event in sorted(events, key=lambda item: item.tte_hours):
            if event.tte_hours <= 0:
                continue
            if max_tte_hours is not None and event.tte_hours > max_tte_hours:
                continue
            return event
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
        earnings_task = self.get_symbol_earnings_batch(symbols)

        economic_events, earnings_map = await asyncio.gather(events_task, earnings_task)

        from typing import cast

        economic_events = cast(List[EconomicEvent], economic_events)
        earnings_events = [
            cast(EarningsEvent, event)
            for event in earnings_map.values()
            if event is not None
        ]

        all_events: List[Union[EconomicEvent, EarningsEvent]] = []
        all_events.extend(economic_events)
        all_events.extend(earnings_events)

        combined = sorted(all_events, key=lambda x: x.tte_hours)
        return combined

    async def update_fedwatch_probability(self) -> None:
        """從 edge scraper 獲取下週 FOMC 最新利率定價機率，並寫入資料庫。"""
        import httpx
        import config
        from database.cache import save_kv_cache
        from database.connection import execute_write_async

        if not getattr(config, "TUNNEL_URL", ""):
            logger.info("未配置 TUNNEL_URL，跳過 FedWatch 爬取。")
            await save_kv_cache("macro_fedwatch_is_fallback", 1)
            return

        try:
            async with httpx.AsyncClient(timeout=15.0) as client:
                res = await client.get(f"{config.TUNNEL_URL}/scrape/macro/fedwatch")
                if res.status_code == 200:
                    payload = res.json()
                    if payload.get("status") == "success":
                        prob = payload["data"].get("probability", 0.72)
                        await execute_write_async(
                            """
                            UPDATE economic_calendar_events
                            SET fedwatch_probability = ?
                            WHERE event LIKE '%FOMC%' OR event LIKE '%Fed Interest Rate%'
                            """,
                            (prob,),
                        )
                        logger.info(
                            f"成功更新 CME FedWatch FOMC 利率維持/加息機率: {prob * 100:.1f}%"
                        )
                        await save_kv_cache("macro_fedwatch_is_fallback", 0)
                        return
        except Exception as e:
            logger.error(f"更新 FedWatch 概率失敗: {e}")

        await save_kv_cache("macro_fedwatch_is_fallback", 1)


# Singleton instance
calendar_service = CalendarService()
