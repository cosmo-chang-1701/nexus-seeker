from typing import Any
import pandas_market_calendars as mcal
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo
import logging

logger = logging.getLogger(__name__)

ny_tz = ZoneInfo("America/New_York")
nyse_calendar = mcal.get_calendar("NYSE")


def get_next_market_target_time(
    reference: str = "open", offset_minutes: int = 0, skip_today: bool = False
) -> datetime | None:
    """獲取下一個市場的目標時間"""
    now = datetime.now(ny_tz)

    # 如果指定跳過今天，則從明天開始找
    start_search = now.date() + timedelta(days=1) if skip_today else now.date()
    end_date = now.date() + timedelta(days=14)
    schedule = nyse_calendar.schedule(start_date=start_search, end_date=end_date)

    if schedule.empty:
        return None

    for index, row in schedule.iterrows():
        if reference == "open":
            raw_target = row["market_open"].to_pydatetime()
        else:
            raw_target = row["market_close"].to_pydatetime()

        target_utc: datetime = raw_target
        # Ensure target_utc is timezone-aware (UTC)
        if target_utc.tzinfo is None:
            target_utc = target_utc.replace(tzinfo=timezone.utc)

        target_ny: datetime = target_utc.astimezone(ny_tz) + timedelta(
            minutes=offset_minutes
        )

        if target_ny > (now - timedelta(seconds=1)):
            logger.info(f"Next market {reference} target: {target_ny}")
            return target_ny

    return None


def get_sleep_seconds(target_time: datetime | None) -> float:
    if not target_time:
        return 3600.0

    sleep_secs = (target_time - datetime.now(ny_tz)).total_seconds()
    return max(0.0, sleep_secs)


def is_market_open() -> Any:
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
    market_open = row["market_open"].tz_convert(ny_tz).to_pydatetime()
    market_close = row["market_close"].tz_convert(ny_tz).to_pydatetime()

    # 5. 判斷當下時間是否落在開盤與收盤之間
    return market_open <= now_ny <= market_close


def get_trading_day_elapsed_fraction(min_fraction: float = 0.05) -> float:
    """回傳「今日交易時段已經過的比例」(0.0-1.0)，供成交量類指標的盤中時段
    進度正規化使用（例如 UOA 的 Volume/OI 比值：OI 是前一交易日收盤的固定值，
    Volume 卻隨盤中時間持續累積，同一個原始比值在開盤 10 分鐘與收盤前 10 分鐘
    代表的「異常程度」並不相同）。

    沿用 `is_market_open()` 相同的 NYSE 行事曆查詢方式（精準處理週末/假日/
    提前收市，如感恩節前夕）。非交易時間（盤前/盤後/週末/假日）或計算失敗時
    一律回傳 1.0——等同不做任何正規化，直接使用原始比值，避免對「非交易時段」
    這個未定義的情境做出誤導性的假設。

    開盤後極短時間內（實際經過比例 < min_fraction）鉗制至 min_fraction（預設
    5%，約開盤後 20 分鐘），避免分母趨近於零時把早盤極少量成交爆量放大成失真
    的巨大倍數，犧牲一點早盤靈敏度換取數值穩定性。
    """
    now_ny = datetime.now(ny_tz)
    try:
        schedule = nyse_calendar.schedule(
            start_date=now_ny.date(), end_date=now_ny.date()
        )
        if schedule.empty:
            return 1.0

        row = schedule.iloc[0]
        market_open = row["market_open"].tz_convert(ny_tz).to_pydatetime()
        market_close = row["market_close"].tz_convert(ny_tz).to_pydatetime()

        if now_ny <= market_open or now_ny >= market_close:
            return 1.0

        total_secs = (market_close - market_open).total_seconds()
        if total_secs <= 0:
            return 1.0

        elapsed_secs = (now_ny - market_open).total_seconds()
        fraction = float(elapsed_secs) / float(total_secs)
        return max(min_fraction, min(1.0, fraction))
    except Exception as e:
        logger.warning(f"計算交易時段進度失敗，回退為 1.0 (不正規化): {e}")
        return 1.0
