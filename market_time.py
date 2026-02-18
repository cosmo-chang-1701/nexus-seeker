import pandas_market_calendars as mcal
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo
import logging

logger = logging.getLogger(__name__)

ny_tz = ZoneInfo("America/New_York")
nyse_calendar = mcal.get_calendar('NYSE')

def get_next_market_target_time(reference="open", offset_minutes=0):
    """獲取下一個市場的目標時間"""
    now = datetime.now(ny_tz)
    end_date = now.date() + timedelta(days=7)
    schedule = nyse_calendar.schedule(start_date=now.date(), end_date=end_date)

    if schedule.empty:
        return None

    for index, row in schedule.iterrows():
        if reference == "open":
            target_utc = row['market_open'].to_pydatetime()
        else:
            target_utc = row['market_close'].to_pydatetime()
            
        # Ensure target_utc is timezone-aware (UTC)
        if target_utc.tzinfo is None:
            target_utc = target_utc.replace(tzinfo=timezone.utc)
            
        target_ny = target_utc.astimezone(ny_tz) + timedelta(minutes=offset_minutes)
        
        if now < target_ny:
            logger.info(f"Next market {reference} target: {target_ny}")
            return target_ny
            
    return None

def get_sleep_seconds(target_time):
    if not target_time:
        return 3600

    sleep_secs = (target_time - datetime.now(ny_tz)).total_seconds()
    return max(0.0, sleep_secs)