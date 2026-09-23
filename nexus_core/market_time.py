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


def get_trading_days_ago_utc(n_trading_days: int, for_purge: bool = False) -> str:
    """回傳「往回數第 `n_trading_days` 個交易日的開盤時刻」之 UTC 字串
    (`'%Y-%m-%d %H:%M:%S'`)，供 SQLite 以 `observed_at >= ?` 做時間窗過濾。

    為什麼用交易日而非日曆日：UOA 只在開盤時段產生。以日曆日回看 5 天，遇到
    週末就只剩 3 個交易日、遇到連假只剩 2 個——同一個「5 日回看窗」在一週內
    不同日子代表的樣本量會相差近一倍，門檻等於隨機漂移。

    `n_trading_days=1` 代表「今天（或最近一個交易日）開盤以來」。

    行事曆查詢失敗時 fail-safe 回退為 `n_trading_days` 個**日曆日**前——這個
    方向是保守的（日曆日窗必然短於或等於同數量的交易日窗），寧可少撈到歷史
    紀錄而讓條件四維持嚴格，也不要因為行事曆異常而意外放寬進場門檻。

    `for_purge=True` 供保留期清理使用，fallback 方向必須反過來：清理的截止點
    越早越安全（寧可少刪），因此退回同樣足以涵蓋連假的 `n * 2 + 10` 日曆日長窗，
    而不是會提前誤刪的 `n` 日曆日短窗。
    """
    now_ny = datetime.now(ny_tz)
    n = max(1, int(n_trading_days))
    try:
        # 往回抓足夠長的日曆窗：n 個交易日最壞情況（連假）約需 n * 2 + 10 天。
        start = now_ny.date() - timedelta(days=n * 2 + 10)
        schedule = nyse_calendar.schedule(start_date=start, end_date=now_ny.date())
        if not schedule.empty:
            opens = list(schedule["market_open"])
            # 只保留已經開盤的交易日：盤前執行時「今天」雖在行事曆內但尚未
            # 產生任何 UOA，把它算進回看窗會實質縮短一天。
            elapsed = [
                o for o in opens if o.tz_convert(ny_tz).to_pydatetime() <= now_ny
            ]
            if elapsed:
                target = elapsed[-min(n, len(elapsed))]
                return str(
                    target.tz_convert(timezone.utc)
                    .to_pydatetime()
                    .strftime("%Y-%m-%d %H:%M:%S")
                )
    except Exception as e:
        logger.warning(f"NYSE 行事曆回看 {n} 個交易日失敗，退回日曆日: {e}")

    fallback_days = (n * 2 + 10) if for_purge else n
    return (datetime.now(timezone.utc) - timedelta(days=fallback_days)).strftime(
        "%Y-%m-%d %H:%M:%S"
    )


def get_last_completed_trading_date(as_of: datetime | None = None) -> str:
    """回傳最近一個**已收盤**交易日的美東日期 (`'YYYY-MM-DD'`)。

    收盤時刻取自 NYSE 行事曆（半日市為 13:00 ET），所以收盤後呼叫會回傳當天，
    盤前或盤中呼叫則回傳前一個交易日，週末與國定假日會自動跳過。`as_of` 若為
    naive datetime，視為美東時間。

    行事曆查詢失敗時，fail-safe 退回「前一個平日」：只能跳過週末、無法辨識
    國定假日，但呼叫端（canonical 日級快照補寫）對一個沒有盤中資料的日期只會
    寫入 0 筆，不會產生錯誤資料。
    """
    if as_of is None:
        now_ny = datetime.now(ny_tz)
    elif as_of.tzinfo is None:
        now_ny = as_of.replace(tzinfo=ny_tz)
    else:
        now_ny = as_of.astimezone(ny_tz)

    try:
        start = now_ny.date() - timedelta(days=20)
        schedule = nyse_calendar.schedule(start_date=start, end_date=now_ny.date())
        if not schedule.empty:
            completed = [
                c
                for c in schedule["market_close"]
                if c.tz_convert(ny_tz).to_pydatetime() <= now_ny
            ]
            if completed:
                return str(completed[-1].tz_convert(ny_tz).strftime("%Y-%m-%d"))
    except Exception as e:
        logger.warning(f"取得最近已收盤交易日失敗，退回前一個平日: {e}")

    fallback = now_ny.date() - timedelta(days=1)
    while fallback.weekday() >= 5:
        fallback -= timedelta(days=1)
    return fallback.strftime("%Y-%m-%d")


def get_session_bounds_utc(
    start_date: Any, end_date: Any
) -> dict[str, tuple[str, str]]:
    """回傳區間內每個 NYSE 交易日的 `{美東日期: (開盤 UTC, 收盤 UTC)}`。

    時間字串格式為 `'%Y-%m-%d %H:%M:%S'`，與 SQLite `CURRENT_TIMESTAMP` 相同，
    可以直接做字串比較。非交易日不會出現在結果中；半日市的收盤時刻為實際的
    13:00 ET。行事曆查詢失敗時直接拋出例外——呼叫端寧可放棄這次重採樣，也不要
    用猜的交易時段寫入規範歷史。
    """
    schedule = nyse_calendar.schedule(start_date=start_date, end_date=end_date)
    bounds: dict[str, tuple[str, str]] = {}
    fmt = "%Y-%m-%d %H:%M:%S"
    for _, row in schedule.iterrows():
        market_open = row["market_open"]
        market_close = row["market_close"]
        trade_date = market_open.tz_convert(ny_tz).strftime("%Y-%m-%d")
        bounds[trade_date] = (
            market_open.tz_convert(timezone.utc).strftime(fmt),
            market_close.tz_convert(timezone.utc).strftime(fmt),
        )
    return bounds
