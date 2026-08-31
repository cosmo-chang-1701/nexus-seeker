"""自選股事件風控上下文（財報/總經事件倒數與風控模式判定）。"""

import asyncio
import logging
from typing import Any

from models.schemas import WatchlistEventContext, WatchlistRiskMode


logger = logging.getLogger(__name__)


def _hours_to_days_text(hours: float) -> str:
    if hours >= 24.0:
        return f"{hours / 24.0:.1f} 天"
    return f"{hours:.1f} 小時"


def _resolve_watchlist_event_mode(
    earnings_tte_hours: float | None, macro_tte_hours: float | None
) -> WatchlistRiskMode:
    if earnings_tte_hours is not None and 0 < earnings_tte_hours <= 72.0:
        return "event-lock"
    if earnings_tte_hours is not None and 0 < earnings_tte_hours <= 168.0:
        return "earnings-guard"
    if macro_tte_hours is not None and 0 < macro_tte_hours <= 48.0:
        return "macro-guard"
    return "normal"


def _build_watchlist_event_summary(
    symbol: str,
    earnings_date: str | None,
    earnings_tte_hours: float | None,
    macro_event: str | None,
    macro_tte_hours: float | None,
    risk_mode: WatchlistRiskMode,
) -> str:
    if risk_mode == "event-lock" and earnings_tte_hours is not None:
        return (
            f"{symbol} 財報倒數 {_hours_to_days_text(earnings_tte_hours)} ｜ "
            "禁做賣方、僅保留保護性 / Debit Spread 類型。"
        )
    if risk_mode == "earnings-guard" and earnings_tte_hours is not None:
        return (
            f"{symbol} 財報將於 {earnings_date or '近期'} 公布 "
            f"(倒數 {_hours_to_days_text(earnings_tte_hours)}) ｜ "
            "先降風險，避免裸賣方與過大口數。"
        )
    if (
        risk_mode == "macro-guard"
        and macro_event is not None
        and macro_tte_hours is not None
    ):
        return (
            f"{macro_event} 倒數 {_hours_to_days_text(macro_tte_hours)} ｜ "
            "先縮口數，優先定義風險的 Debit Spread / 保護性部位。"
        )
    return "未偵測到近期需調整參數的重大事件。"


async def build_watchlist_event_context(
    symbol: str,
    *,
    earnings_event: Any | None = None,
    macro_event: Any | None = None,
) -> WatchlistEventContext:
    from services.calendar_service import calendar_service

    if earnings_event is None or macro_event is None:
        fetched_earnings, fetched_macro = await asyncio.gather(
            calendar_service.get_symbol_earnings(symbol),
            calendar_service.get_next_high_impact_event(days=7),
        )
        if earnings_event is None:
            earnings_event = fetched_earnings
        if macro_event is None:
            macro_event = fetched_macro

    earnings_date = getattr(earnings_event, "date", None)
    earnings_tte_hours = getattr(earnings_event, "tte_hours", None)
    macro_name = getattr(macro_event, "event", None)
    macro_time = getattr(macro_event, "time", None)
    macro_tte_hours = getattr(macro_event, "tte_hours", None)

    is_macro_released = False
    macro_release_time = None
    if macro_time and macro_name:
        try:
            from datetime import datetime
            from zoneinfo import ZoneInfo

            cleaned_time = macro_time.replace("Z", "+00:00")
            macro_release_time = datetime.fromisoformat(cleaned_time).astimezone(
                ZoneInfo("Asia/Taipei")
            )
            current_cst = datetime.now(ZoneInfo("Asia/Taipei"))
            if current_cst >= macro_release_time:
                is_macro_released = True
        except Exception as e:
            logger.warning(f"Error parsing macro event time {macro_time}: {e}")

    if is_macro_released and macro_release_time is not None:
        macro_tte_hours = None
        risk_mode = _resolve_watchlist_event_mode(earnings_tte_hours, None)
        release_time_str = macro_release_time.strftime("%H:%M")
        summary = (
            f"{macro_name} 數據已於 {release_time_str} CST 正式公布。"
            f"宏觀不確定性逐步落地，轉入盤中實體重力回歸監控。"
        )
    else:
        risk_mode = _resolve_watchlist_event_mode(earnings_tte_hours, macro_tte_hours)
        summary = _build_watchlist_event_summary(
            symbol,
            earnings_date,
            earnings_tte_hours,
            macro_name,
            macro_tte_hours,
            risk_mode,
        )

    return WatchlistEventContext(
        earnings_date=earnings_date,
        earnings_tte_hours=earnings_tte_hours,
        macro_event=macro_name,
        macro_event_time=macro_time,
        macro_tte_hours=macro_tte_hours,
        risk_mode=risk_mode,
        summary=summary,
    )
