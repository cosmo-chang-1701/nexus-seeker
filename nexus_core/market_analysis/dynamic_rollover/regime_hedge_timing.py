"""避險推播的時點／事件判定（葉模組，只讀不寫）。

- 開盤 30 分鐘「預警觀察」窗口（GEX／OI 仍是前一交易日快取）。
- 總經事件（FOMC／CPI／NFP／核心 PCE）8 小時內凍結新建 −3x 訊號。
- 持股財報日讀取。
"""

import asyncio
import logging
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from typing import Any, Iterable, Mapping, Optional, Sequence
from zoneinfo import ZoneInfo

from market_time import get_options_chain_recompute_ready_at

logger = logging.getLogger(__name__)

_NY_TZ = ZoneInfo("America/New_York")

MACRO_FREEZE_TERMS: tuple[str, ...] = (
    "FOMC",
    "FED DECISION",
    "INTEREST RATE",
    "CPI",
    "NFP",
    "NONFARM",
    "PCE",
    # 資料庫存的是 Edge Scraper 翻成中文的名稱；不用「非農」以免命中「小非農」
    "利率決策",
    "非農就業",
)
MACRO_FREEZE_LOOKAHEAD_HOURS: float = 8.0


@dataclass(frozen=True)
class MacroFreezeStatus:
    frozen: bool
    calendar_known: bool  # False：所需月份快取不完整
    event_name: Optional[str] = None
    event_time_utc: Optional[datetime] = None

    def reason_text(self) -> Optional[str]:
        if not self.frozen:
            return None
        if not self.calendar_known or self.event_time_utc is None:
            return "總經日曆快取不完整，保守凍結新建 −3x 反向 ETF"
        et = self.event_time_utc.astimezone(_NY_TZ).strftime("%H:%M")
        return f"{self.event_name} 將於 {et} ET 公布（8 小時內），凍結新建 −3x 反向 ETF"


def is_preliminary_window(now: Optional[datetime] = None) -> bool:
    """開盤 30 分內或盤外：數據尚非最終依據，只發「預警觀察」。"""
    return get_options_chain_recompute_ready_at(now) is not None


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


def find_macro_freeze_event(
    events: Iterable[Mapping[str, Any]],
    now_utc: datetime,
    lookahead_hours: float = MACRO_FREEZE_LOOKAHEAD_HOURS,
) -> Optional[tuple[str, datetime]]:
    """回傳 lookahead 內最早一筆命中關鍵字的事件（名稱大寫, UTC 時間）。"""
    if now_utc.tzinfo is None:
        now_utc = now_utc.replace(tzinfo=timezone.utc)
    horizon = now_utc + timedelta(hours=lookahead_hours)
    best: Optional[tuple[str, datetime]] = None
    for evt in events:
        name = str(evt.get("event") or "").upper()
        if not any(term in name for term in MACRO_FREEZE_TERMS):
            continue
        evt_dt = _parse_event_time_utc(evt.get("event_time"))
        if evt_dt is None or not (now_utc < evt_dt <= horizon):
            continue
        if best is None or evt_dt < best[1]:
            best = (name, evt_dt)
    return best


def _month_keys_ny(start_utc: datetime, end_utc: datetime) -> list[str]:
    start = start_utc.astimezone(_NY_TZ).date()
    end = end_utc.astimezone(_NY_TZ).date()
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


def _load_macro_freeze_sync(now_utc: datetime) -> MacroFreezeStatus:
    # 注意：get_macro_events_between 會吞 DB 例外並回傳 []；若月份狀態讀取成功
    # 但事件讀取失敗會 fail-open（calendar_cache.py 不可改，僅記錄）。
    from database.calendar_cache import (
        get_macro_events_between,
        get_macro_month_status,
    )

    end_utc = now_utc + timedelta(hours=MACRO_FREEZE_LOOKAHEAD_HOURS)
    for key in _month_keys_ny(now_utc, end_utc):
        if get_macro_month_status(key) is None:
            return MacroFreezeStatus(frozen=True, calendar_known=False)
    events = get_macro_events_between(
        now_utc.astimezone(timezone.utc).date().isoformat(),
        end_utc.astimezone(timezone.utc).date().isoformat(),
    )
    hit = find_macro_freeze_event(events, now_utc)
    if hit is None:
        return MacroFreezeStatus(frozen=False, calendar_known=True)
    return MacroFreezeStatus(
        frozen=True, calendar_known=True, event_name=hit[0], event_time_utc=hit[1]
    )


async def load_macro_freeze_status(
    now_utc: Optional[datetime] = None,
) -> MacroFreezeStatus:
    """讀快取判斷是否凍結；日曆不完整或讀取失敗一律 fail-closed（凍結）。"""
    now = now_utc or datetime.now(timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    try:
        return await asyncio.to_thread(_load_macro_freeze_sync, now)
    except Exception as e:
        logger.warning("讀取總經日曆快取失敗，保守凍結 −3x: %s", e)
        return MacroFreezeStatus(frozen=True, calendar_known=False)


def _parse_earnings_date(raw: Any) -> Optional[date]:
    try:
        return datetime.strptime(str(raw)[:10], "%Y-%m-%d").date()
    except ValueError:
        return None


def _load_earnings_sync(symbols: list[str]) -> dict[str, Optional[date]]:
    from database.calendar_cache import get_cached_earnings_many

    out: dict[str, Optional[date]] = {}
    for sym, row in get_cached_earnings_many(symbols).items():
        raw = row.get("earnings_date")
        if raw is None:
            out[sym.upper()] = None
            continue
        parsed = _parse_earnings_date(raw)
        if parsed is not None:
            out[sym.upper()] = parsed
    return out


async def load_earnings_dates(symbols: Sequence[str]) -> dict[str, Optional[date]]:
    """持股財報日（None＝無財報日，例如 ETF；無法解析者不放入）。"""
    try:
        return await asyncio.to_thread(_load_earnings_sync, list(symbols))
    except Exception as e:
        logger.warning("讀取財報日快取失敗: %s", e)
        return {}
