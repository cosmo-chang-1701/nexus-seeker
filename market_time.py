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

def is_market_open():
    """
    判斷當下這一秒，美股是否正在常規交易時間內。
    (精準避開週末、國定假日，以及如感恩節前夕的提前收市)
    """
    # 1. 取得當下的美東時間
    now_ny = datetime.now(ny_tz)
    
    # 2. 查詢「今天」的紐交所行事曆
    schedule = nyse_calendar.schedule(start_date=now_ny.date(), end_date=now_ny.date())
    
    # 3. 如果回傳為空，代表今天是週末或國定假日休市
    if schedule.empty:
        return False 
        
    # 4. 取得今天的確切開盤與收盤時間，並強制轉換為美東時區
    row = schedule.iloc[0]
    market_open = row['market_open'].tz_convert(ny_tz).to_pydatetime()
    market_close = row['market_close'].tz_convert(ny_tz).to_pydatetime()
    
    # 5. 判斷當下時間是否落在開盤與收盤之間
    return market_open <= now_ny <= market_close