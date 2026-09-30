"""/x 量化雷達「靜默期避讓」(`avoid_silent_period`) 的事件窗判定。

語意：面板參數 `silent_period_days` = N 時，**未來 N 天內（含今日，美東日期）**
有財報或高影響總經事件的標的即排除。

資料來源（皆為既有 SQLite 快取，不發出任何 API 請求）：
- 財報日：`earnings_calendar_cache`（由 `CalendarService.get_symbol_earnings()`
  等既有流程寫入），以 `get_cached_earnings_many()` 單次批次讀取。
- 總經事件：`economic_calendar_events`（由 `CalendarService` 月度快取寫入），以
  `get_macro_events_between()` 讀取；總經事件為全市場事件，一旦窗內存在，所有
  標的皆視為處於靜默期。

退路：拿不到事件日期時，沿用 `iv_data` 的 `has_earnings_event` / `has_macro_event`
布林值（由 `market_analysis/sentiment/iv_metrics.py` 以固定 14 天窗計算）：
- 財報：該標的沒有快取列，或快取的財報日已過期（早於今日，代表快取尚未更新下一期）。
- 總經：事件窗涵蓋的任一月份尚未建立月度快取（`economic_calendar_month_cache` 無紀錄）。
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta, timezone
from typing import Any, Optional
from zoneinfo import ZoneInfo

logger = logging.getLogger(__name__)

_NY_TZ = ZoneInfo("America/New_York")
DEFAULT_SILENT_PERIOD_DAYS = 5

# 與 iv_metrics.py 的 has_macro_event 判定一致的高影響事件關鍵字
_HIGH_IMPACT_TERMS = ("FOMC", "INTEREST RATE", "CPI", "NFP", "FED DECISION")


@dataclass(frozen=True)
class SilentPeriodContext:
    today: date
    window_days: int
    # None：總經日曆快取不完整，退回 iv_data.has_macro_event
    macro_event_in_window: Optional[bool]
    # 大寫 symbol → 快取中的財報日；值為 None 表示快取列存在但無財報日
    # （例如 ETF），視為確定無財報。無快取列的標的不在此 dict 中。
    earnings_dates: dict[str, Optional[date]] = field(default_factory=dict)

    @property
    def window_end(self) -> date:
        return self.today + timedelta(days=self.window_days)


def normalize_silent_period_days(raw: Any) -> int:
    try:
        days = int(raw)
    except (TypeError, ValueError):
        return DEFAULT_SILENT_PERIOD_DAYS
    return max(0, days)


def _is_high_impact(evt: dict[str, Any]) -> bool:
    name = str(evt.get("event") or "").upper()
    return str(evt.get("impact") or "").upper() == "HIGH" or any(
        term in name for term in _HIGH_IMPACT_TERMS
    )


def _parse_event_time_utc(raw: Any) -> Optional[datetime]:
    if not raw:
        return None
    try:
        dt = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def _month_keys(start: date, end: date) -> list[str]:
    keys: list[str] = []
    cursor = date(start.year, start.month, 1)
    while cursor <= end:
        keys.append(cursor.strftime("%Y-%m"))
        cursor = (
            date(cursor.year + 1, 1, 1)
            if cursor.month == 12
            else date(cursor.year, cursor.month + 1, 1)
        )
    return keys


def _parse_earnings_date(raw: Any) -> Optional[date]:
    if not raw:
        return None
    try:
        return datetime.strptime(str(raw)[:10], "%Y-%m-%d").date()
    except ValueError:
        return None


def _load_context_sync(
    symbols: list[str], window_days: int, now_utc: datetime
) -> SilentPeriodContext:
    from database.calendar_cache import (
        get_cached_earnings_many,
        get_macro_events_between,
        get_macro_month_status,
    )

    today = now_utc.astimezone(_NY_TZ).date()
    window_end = today + timedelta(days=window_days)

    # 財報日（美東日期）
    earnings_dates: dict[str, Optional[date]] = {}
    for sym, row in get_cached_earnings_many(symbols).items():
        raw = row.get("earnings_date")
        if raw is None:
            earnings_dates[sym] = None
            continue
        parsed = _parse_earnings_date(raw)
        if parsed is not None:
            earnings_dates[sym] = parsed
        # 無法解析的日期不放入 dict，交由布林值退路判斷

    # 總經事件：event_time 為 UTC ISO；窗口終點為美東 window_end 當日 23:59:59。
    window_end_utc = datetime.combine(
        window_end, time(23, 59, 59), tzinfo=_NY_TZ
    ).astimezone(timezone.utc)
    start_utc_date = now_utc.astimezone(timezone.utc).date()
    end_utc_date = window_end_utc.date()

    macro_in_window: Optional[bool]
    # 月度快取以美東月份為 key（見 CalendarService.prefetch_monthly_macro_cache），
    # 因此以美東日期區間判斷快取是否完整。
    months = _month_keys(today, window_end)
    if all(get_macro_month_status(key) is not None for key in months):
        events = get_macro_events_between(
            start_utc_date.isoformat(), end_utc_date.isoformat()
        )
        macro_in_window = False
        for evt in events:
            evt_dt = _parse_event_time_utc(evt.get("event_time"))
            if evt_dt is None or not (now_utc <= evt_dt <= window_end_utc):
                continue
            if _is_high_impact(evt):
                macro_in_window = True
                break
    else:
        macro_in_window = None

    return SilentPeriodContext(
        today=today,
        window_days=window_days,
        macro_event_in_window=macro_in_window,
        earnings_dates=earnings_dates,
    )


async def load_silent_period_context(
    symbols: list[str], window_days: Any, now_utc: Optional[datetime] = None
) -> SilentPeriodContext:
    """讀取靜默期判定所需的財報日與總經事件（SQLite 快取，於執行緒中讀取）。"""
    days = normalize_silent_period_days(window_days)
    now = now_utc or datetime.now(timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    return await asyncio.to_thread(_load_context_sync, symbols, days, now)


def _iv_flag(iv_data: Any, key: str) -> bool:
    if not iv_data:
        return False
    if isinstance(iv_data, dict):
        return bool(iv_data.get(key, False))
    return bool(getattr(iv_data, key, False))


def is_in_silent_period(result: dict[str, Any], ctx: SilentPeriodContext) -> bool:
    """標的在未來 N 天內有財報或高影響總經事件時回傳 True（應排除）。"""
    iv_data = result.get("iv_data")
    sym = str(result.get("symbol") or "").upper()

    if sym in ctx.earnings_dates:
        earn_date = ctx.earnings_dates[sym]
        if earn_date is None:
            earnings_hit = False
        elif earn_date >= ctx.today:
            earnings_hit = earn_date <= ctx.window_end
        else:
            # 快取中的財報日已過，下一期日期未知 → 退回布林值
            earnings_hit = _iv_flag(iv_data, "has_earnings_event")
    else:
        earnings_hit = _iv_flag(iv_data, "has_earnings_event")

    if ctx.macro_event_in_window is not None:
        macro_hit = ctx.macro_event_in_window
    else:
        macro_hit = _iv_flag(iv_data, "has_macro_event")

    return earnings_hit or macro_hit
