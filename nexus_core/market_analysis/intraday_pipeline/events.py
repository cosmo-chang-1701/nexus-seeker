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


def _is_opex_week() -> bool:
    """判斷美東當前週是否為月度期權結算週（OPEX Week，含每月第 3 個星期五）。"""
    from datetime import date, datetime
    from zoneinfo import ZoneInfo

    ny_today = datetime.now(ZoneInfo("America/New_York")).date()
    first_day = date(ny_today.year, ny_today.month, 1)
    first_friday = 1 + (4 - first_day.weekday()) % 7
    third_friday = first_friday + 14
    opex_friday = date(ny_today.year, ny_today.month, third_friday)
    monday_opex = date.fromordinal(opex_friday.toordinal() - opex_friday.weekday())
    sunday_opex = date.fromordinal(monday_opex.toordinal() + 6)
    return monday_opex <= ny_today <= sunday_opex


def _resolve_watchlist_event_mode(
    earnings_tte_hours: float | None,
    macro_tte_hours: float | None,
    is_earnings_released: bool = False,
) -> WatchlistRiskMode:
    if is_earnings_released:
        earnings_tte_hours = None

    has_event_lock = earnings_tte_hours is not None and 0 < earnings_tte_hours <= 72.0
    has_earnings_guard = (
        earnings_tte_hours is not None and 0 < earnings_tte_hours <= 168.0
    )
    has_macro_guard = macro_tte_hours is not None and -2.0 <= macro_tte_hours <= 48.0

    # 1. 72 小時內即期財報強制最高限制 event-lock（禁做賣方）
    if has_event_lock:
        return "event-lock"

    # 2. 宏觀事件優先級判定 (修復 ISSUE-1.5)：
    # 當宏觀事件處於 48 小時內（或公布後 2 小時消化期），若遠期財報在 72~168 小時（3~7 天），
    # 宏觀事件在時間尺度上更為緊迫（例如 1 小時後的 FOMC vs 6 天後的財報），
    # 優先返回 macro-guard，防止即期總經海嘯被遠期財報掩蓋
    if has_macro_guard:
        if has_earnings_guard:
            effective_macro_tte = (
                max(0.0, macro_tte_hours) if macro_tte_hours is not None else 999.0
            )
            if (
                earnings_tte_hours is not None
                and effective_macro_tte <= earnings_tte_hours
            ):
                return "macro-guard"
        else:
            return "macro-guard"

    if has_earnings_guard:
        return "earnings-guard"

    return "normal"


def _build_watchlist_event_summary(
    symbol: str,
    earnings_date: str | None,
    earnings_tte_hours: float | None,
    macro_event: str | None,
    macro_tte_hours: float | None,
    risk_mode: WatchlistRiskMode,
) -> str:
    has_earnings_guard = (
        earnings_tte_hours is not None and 0 < earnings_tte_hours <= 168.0
    )
    has_macro_guard = macro_tte_hours is not None and -2.0 <= macro_tte_hours <= 48.0

    if risk_mode == "event-lock" and earnings_tte_hours is not None:
        base_summary = (
            f"{symbol} 財報倒數 {_hours_to_days_text(earnings_tte_hours)} ｜ "
            "禁做賣方、僅保留保護性 / Debit Spread 類型。"
        )
        if has_macro_guard and macro_event and macro_tte_hours is not None:
            macro_info = (
                f"{macro_event} 數據公布消化中 (T{macro_tte_hours:+.1f}h)"
                if macro_tte_hours <= 0.0
                else f"{macro_event} 倒數 {_hours_to_days_text(macro_tte_hours)}"
            )
            return f"{base_summary}（⚠️ 同步面臨 {macro_info} 重磅總經風險）"
        return base_summary

    if (
        risk_mode == "macro-guard"
        and macro_event is not None
        and macro_tte_hours is not None
    ):
        if macro_tte_hours <= 0.0:
            macro_summary = (
                f"{macro_event} 數據已公布（消化冷卻期中，T{macro_tte_hours:+.1f}h）｜ "
                "鮑爾記者會與市場劇烈消化政策震盪期，先縮口數，維持防禦模式。"
            )
        else:
            macro_summary = (
                f"{macro_event} 倒數 {_hours_to_days_text(macro_tte_hours)} ｜ "
                "先縮口數，優先定義風險的 Debit Spread / 保護性部位。"
            )
        if has_earnings_guard and earnings_tte_hours is not None:
            macro_summary = (
                f"{macro_summary}（另留意：{symbol} 財報將於 {earnings_date or '近期'} "
                f"倒數 {_hours_to_days_text(earnings_tte_hours)}）"
            )
        return macro_summary

    if risk_mode == "earnings-guard" and earnings_tte_hours is not None:
        return (
            f"{symbol} 財報將於 {earnings_date or '近期'} 公布 "
            f"(倒數 {_hours_to_days_text(earnings_tte_hours)}) ｜ "
            "先降風險，避免裸賣方與過大口數。"
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
    is_earnings_released = getattr(earnings_event, "is_released", False)
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

    effective_macro_tte = (
        macro_tte_hours
        if (macro_tte_hours is not None and macro_tte_hours >= -2.0)
        else None
    )
    risk_mode = _resolve_watchlist_event_mode(
        earnings_tte_hours,
        effective_macro_tte,
        is_earnings_released=is_earnings_released,
    )

    if is_earnings_released and earnings_date:
        earnings_hour = getattr(earnings_event, "hour", None)
        timing_str = (
            "盤前"
            if earnings_hour == "bmo"
            else "盤後"
            if earnings_hour == "amc"
            else ""
        )
        if is_macro_released and macro_release_time is not None:
            if macro_tte_hours is not None and macro_tte_hours >= -2.0:
                summary = (
                    f"{symbol} 今日{timing_str}財報與 {macro_name} 數據均已公布；"
                    f"惟 {macro_name} 仍在公布後 2 小時市場消化震盪期，維持防禦模式。"
                )
            else:
                macro_tte_hours = None
                summary = (
                    f"{symbol} 今日{timing_str}財報與 {macro_name} 數據均已公布。"
                    f"重大事件不確定性已落地，轉入盤中實體重力回歸監控。"
                )
        else:
            if (
                risk_mode == "macro-guard"
                and macro_name
                and macro_tte_hours is not None
            ):
                if macro_tte_hours <= 0.0:
                    summary = (
                        f"{symbol} 今日{timing_str}財報已公布；"
                        f"惟 {macro_name} 數據已公布（消化冷卻期中），"
                        "鮑爾記者會與市場劇烈消化政策震盪期，維持防禦部位。"
                    )
                else:
                    summary = (
                        f"{symbol} 今日{timing_str}財報已公布；"
                        f"惟 {macro_name} 倒數 {_hours_to_days_text(macro_tte_hours)}，"
                        "先縮口數，優先定義風險的 Debit Spread / 保護性部位。"
                    )
            else:
                summary = (
                    f"{symbol} 今日{timing_str}財報已正式公布。"
                    f"重大事件風險已落地定價，轉入盤中實體重力回歸監控。"
                )
    elif is_macro_released and macro_release_time is not None:
        release_time_str = macro_release_time.strftime("%H:%M")
        if risk_mode == "event-lock":
            summary = (
                f"{symbol} 財報倒數 {_hours_to_days_text(earnings_tte_hours or 0.0)} ｜ "
                f"禁做賣方。（註：{macro_name} 數據已於 {release_time_str} CST 正式公布）"
            )
        elif macro_tte_hours is not None and macro_tte_hours >= -2.0:
            summary = (
                f"{macro_name} 數據已於 {release_time_str} CST 正式公布（消化冷卻期中）。"
                f"鮑爾記者會與市場劇烈消化政策震盪期，先縮口數，維持防禦部位。"
            )
            if earnings_tte_hours is not None and 0 < earnings_tte_hours <= 168.0:
                summary += f"（另留意：{symbol} 財報倒數 {_hours_to_days_text(earnings_tte_hours)}）"
        else:
            macro_tte_hours = None
            if risk_mode == "earnings-guard":
                summary = (
                    f"{symbol} 財報將於 {earnings_date or '近期'} 公布 "
                    f"(倒數 {_hours_to_days_text(earnings_tte_hours or 0.0)}) ｜ "
                    f"先降風險。（{macro_name} 數據已於 {release_time_str} CST 正式公布）"
                )
            else:
                summary = (
                    f"{macro_name} 數據已於 {release_time_str} CST 正式公布。"
                    f"宏觀不確定性逐步落地，轉入盤中實體重力回歸監控。"
                )
    else:
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
        is_opex_week=_is_opex_week(),
        risk_mode=risk_mode,
        summary=summary,
    )
