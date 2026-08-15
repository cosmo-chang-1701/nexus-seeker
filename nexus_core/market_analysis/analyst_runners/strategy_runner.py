"""Next-day strategy and FOMC escape-window analysis runners."""

from __future__ import annotations
from typing import Any

import logging
import math
import sqlite3
from datetime import date, datetime, timezone, timedelta
from typing import Optional

import discord

from config import get_vix_tier
from market_analysis.sentiment_engine import SentimentEngine
from services.market_data_service import get_vix_term_structure

logger = logging.getLogger(__name__)


def _get_tw_time_str() -> str:
    now_tw = datetime.now(timezone.utc).astimezone(timezone(timedelta(hours=8)))
    return now_tw.strftime("[%H:%M UTC+8]")


def _get_period_label(day: int) -> str:
    if day <= 10:
        return "上旬"
    elif day <= 20:
        return "中旬"
    else:
        return "下旬"


async def run_next_day_strategy(fetch_macro_fn: Any) -> str:
    """Build next-day tactical strategy report string.

    Args:
        fetch_macro_fn: Async callable that returns a macro data dict.
                        Typically ``AnalystAgent._fetch_macro_data``.
    """
    time_str = _get_tw_time_str()
    macro_data = await fetch_macro_fn()
    vix = macro_data.get("vix", 0.0) if isinstance(macro_data, dict) else macro_data[0]
    tier = get_vix_tier(vix)
    tier_display = f"{tier.get('emoji', '')} {tier.get('name', 'Unknown')}"
    vix_display = f"{vix:.2f}" if not math.isnan(vix) else "N/A (Using Default)"

    try:
        vts_data = await get_vix_term_structure()
        vts_ratio = vts_data.get("vts_ratio", 0.0)
        vts_state = vts_data.get("vts_state", "UNKNOWN")
        vix_front = vts_data.get("vix_front")
        vix_back = vts_data.get("vix_back")
        if (
            vts_state == "UNKNOWN"
            or not vts_data.get("is_valid", True)
            or vts_ratio <= 0.0
        ):
            vts_display = "取得失敗 (數據未更新)"
        else:
            vts_detail = (
                f" (VIX/VIX3M: {vix_front:.2f}/{vix_back:.2f})"
                if (vix_front is not None and vix_back is not None)
                else ""
            )
            vts_display = f"{vts_ratio:.3f} ({vts_state}){vts_detail}"
    except Exception as e:
        logger.error(f"獲取 VIX 期限結構失敗: {e}")
        vts_display = "取得失敗 (Using Default)"

    try:
        skew_data = await SentimentEngine.calculate_skew("SPY")
        skew_val = skew_data.get("skew", 0.0)
        skew_state = skew_data.get("state", "N/A")
        skew_display = f"{skew_val}% ({skew_state})"
    except Exception as e:
        logger.error(f"計算 SPY Skew Index 失敗: {e}")
        skew_display = "取得失敗 (Using Default)"

    report = (
        f"**{time_str} 次日策略制定**\n"
        "--------------------------------------------------\n"
        f"**市場狀態指標：**\n"
        f"• 當前 VIX: {vix_display} ({tier_display})\n"
        f"• VIX 期限結構 (VTS): {vts_display}\n"
        f"• SPY 偏態指數 (Skew): {skew_display}\n\n"
        "**戰術建議：**\n"
    )
    if vix < 15:
        report += "⚠️ 市場處於休眠期 (Dormant)。強制拒絕所有 STO 訊號。"
    elif vix >= 35:
        report += (
            "🚨 市場處於極度恐慌 (All-In)。繞過市場政權阻尼，啟用 1/2 Kelly 覆寫。"
        )
    else:
        report += "✅ 已設定標準量化掃描參數。NRO 保證金限制正常運作。"

    return report


def _shift_business_days(start_date: date, num_days: int) -> date:
    """Shift date by a number of business days (positive or negative)."""
    if num_days == 0:
        return start_date
    current = start_date
    step = 1 if num_days > 0 else -1
    remaining = abs(num_days)
    while remaining > 0:
        current += timedelta(days=step)
        if current.weekday() < 5:  # Monday to Friday (0-4)
            remaining -= 1
    return current


def _find_next_window_month(cur_month: int) -> int:
    """Find the next cyclical liquidity/OpEx month (Mar, Jun, Sep, Dec)."""
    quarter_months = [3, 6, 9, 12]
    for qm in quarter_months:
        if qm >= cur_month:
            return qm
    return 3  # next year March


def _resolve_escape_window(
    start_setting: str,
    end_setting: str,
    ref_date: date,
) -> tuple[date, date, bool, str]:
    """Resolve escape window dates, auto-rolling to next quarterly cycle if expired."""
    try:
        start_m, start_d = map(int, start_setting.split("-"))
        end_m, end_d = map(int, end_setting.split("-"))
    except Exception:
        start_m, start_d = 7, 15
        end_m, end_d = 7, 31

    cur_year = ref_date.year
    orig_label = (
        f"{start_m}月{_get_period_label(start_d)}至{end_m}月{_get_period_label(end_d)}"
    )

    # Determine if current setting is expired (more than 7 days past end date)
    def _safe_date(year: int, month: int, day: int) -> date:
        import calendar

        max_day = calendar.monthrange(year, month)[1]
        return date(year, month, min(day, max_day))

    user_start_date = _safe_date(cur_year, start_m, start_d)
    user_end_date = _safe_date(cur_year, end_m, end_d)

    was_auto_rolled = False
    if ref_date > user_end_date + timedelta(days=7):
        was_auto_rolled = True
        rolled_month = _find_next_window_month(ref_date.month)
        roll_year = cur_year if rolled_month >= ref_date.month else cur_year + 1
        # Maintain user day offset preferences (e.g. 15th to end of month)
        user_start_date = _safe_date(roll_year, rolled_month, start_d)
        user_end_date = _safe_date(roll_year, rolled_month, end_d)

    return user_start_date, user_end_date, was_auto_rolled, orig_label


async def run_fomc_escape_window_analysis(
    user_id: int,
) -> Optional[discord.Embed]:
    """Dynamically compute Multi-Factor Macro Liquidity Escape Matrix and return a styled Embed."""
    import config

    # 1. 取得 FedWatch 概率
    prob = 0.72
    is_fallback = False
    try:
        from database.cache import get_kv_cache

        fedwatch_fallback_val = get_kv_cache("macro_fedwatch_is_fallback")
        if fedwatch_fallback_val is None or int(fedwatch_fallback_val) == 1:
            is_fallback = True

        with sqlite3.connect(config.DB_NAME) as conn:
            conn.row_factory = sqlite3.Row
            cursor = conn.cursor()
            cursor.execute(
                """
                SELECT fedwatch_probability
                FROM economic_calendar_events
                WHERE event LIKE '%FOMC%' OR event LIKE '%Fed Interest Rate%'
                ORDER BY event_time ASC
                LIMIT 1
                """
            )
            row = cursor.fetchone()
            if row and row["fedwatch_probability"] is not None:
                prob = row["fedwatch_probability"]
            else:
                is_fallback = True
    except Exception as e:
        logger.warning(f"查詢 SQLite FOMC FedWatch 概率失敗: {e}")
        is_fallback = True

    # 2. 載入使用者自訂逃頂窗口與自動滾動判定
    from database.user_settings import get_full_user_context

    ctx = get_full_user_context(user_id)
    start_setting = ctx.escape_window_start if ctx else "07-15"
    end_setting = ctx.escape_window_end if ctx else "07-31"

    now_tw = datetime.now(timezone.utc).astimezone(timezone(timedelta(hours=8)))
    ref_date = now_tw.date()

    start_date, end_date, was_auto_rolled, orig_label = _resolve_escape_window(
        start_setting, end_setting, ref_date
    )

    # 3. 收集四大宏觀因子
    from database.cache import get_kv_cache

    tightening_score = 0
    easing_score = 0
    factors_summary: list[tuple[str, str]] = []

    # Factor 1: FedWatch 利率定價
    if prob > 0.70:
        tightening_score += 1
        f1_val = f"\u001b[1;31m🚨 鷹派/維持高位 ({prob * 100:.1f}%)\u001b[0m"
    elif prob <= 0.40:
        easing_score += 1
        f1_val = f"\u001b[1;32m🟢 降息預期確立 ({prob * 100:.1f}%)\u001b[0m"
    else:
        f1_val = f"\u001b[1;33m🟡 均衡定價 ({prob * 100:.1f}%)\u001b[0m"
    factors_summary.append(("FOMC 利率定價 (FedWatch)", f1_val))

    # Factor 2: 通膨與能源 (CPI / WTI)
    cpi_actual = get_kv_cache("macro_cpi_actual")
    cpi_expected = get_kv_cache("macro_cpi_expected")
    cpi_dev = (
        cpi_actual - cpi_expected
        if (cpi_actual is not None and cpi_expected is not None)
        else (get_kv_cache("macro_cpi_deviation") or 0.0)
    )
    wti = get_kv_cache("macro_wti") or 75.0

    if (cpi_dev > 0.1) or (wti > 85.0):
        tightening_score += 1
        f2_val = f"\u001b[1;31m🚨 通膨偏高 (WTI ${wti:.1f}, CPI偏差 {cpi_dev:+.2f}%)\u001b[0m"
    elif (cpi_dev <= 0.0) and (wti <= 80.0):
        easing_score += 1
        f2_val = f"\u001b[1;32m🟢 通膨平穩 (WTI ${wti:.1f}, CPI偏差 {cpi_dev:+.2f}%)\u001b[0m"
    else:
        f2_val = f"\u001b[1;33m🟡 通膨受控 (WTI ${wti:.1f}, CPI偏差 {cpi_dev:+.2f}%)\u001b[0m"
    factors_summary.append(("通膨與油價壓力 (CPI/WTI)", f2_val))

    # Factor 3: VIX 期限結構 (VTS)
    vts_ratio = 0.88
    is_vts_valid = False
    try:
        vts_data = await get_vix_term_structure()
        if vts_data.get("is_valid", False) and vts_data.get("vts_state") != "UNKNOWN":
            vts_ratio = vts_data.get("vts_ratio", 0.88)
            is_vts_valid = True
    except Exception as e:
        logger.warning(f"取得 VTS 期限結構失敗: {e}")

    if not is_vts_valid:
        f3_val = "⚪ 數據未更新 (使用中性預設)"
    elif vts_ratio >= 1.0:
        tightening_score += 1
        f3_val = f"\u001b[1;31m🚨 期限倒掛 (VTS: {vts_ratio:.3f})\u001b[0m"
    elif vts_ratio < 0.90:
        easing_score += 1
        f3_val = f"\u001b[1;32m🟢 正價差健康 (VTS: {vts_ratio:.3f})\u001b[0m"
    else:
        f3_val = f"\u001b[1;33m🟡 期限正常 (VTS: {vts_ratio:.3f})\u001b[0m"
    factors_summary.append(("恐慌期限結構 (VIX Term)", f3_val))

    # Factor 4: 大盤 Gamma 翻轉線與微觀結構
    is_negative_gamma = bool(get_kv_cache("macro_short_gamma_critical"))
    if is_negative_gamma:
        tightening_score += 1
        f4_val = "\u001b[1;31m🚨 負 Gamma 踩踏加速區\u001b[0m"
    else:
        easing_score += 1
        f4_val = "\u001b[1;32m🟢 正 Gamma 護航區\u001b[0m"
    factors_summary.append(("大盤微觀結構 (SPY GEX)", f4_val))

    # 4. 三階矩陣狀態評估 (使用統一的宏觀流動性矩陣引擎)
    from market_analysis.index_microstructure import evaluate_escape_window_regime

    (
        tightening_score,
        easing_score,
        direction,
        shift_days,
        tier_title,
        _,
    ) = evaluate_escape_window_regime(
        prob=prob,
        cpi_dev=cpi_dev,
        wti=wti,
        vts_ratio=vts_ratio if is_vts_valid else 0.88,
        is_negative_gamma=is_negative_gamma,
    )

    if direction == "前移":
        tactical_directive = (
            "🚨 **提前防禦撤退**：宏觀流動性收緊與估值回殺風險加劇，多頭反彈窗口受限，"
            "建議於窗口初段逢高分批減倉、收緊防守停損線並全面封鎖裸賣策略。"
        )
        reason = (
            f"宏觀流動性矩陣偵測到 {tightening_score} 項緊縮特徵（如 FedWatch 維持高利率機率達 {prob * 100:.1f}% 或通膨/結構承壓）。"
            f"為防範估值回殺與流動性衰竭，系統自動將自訂反彈逃頂窗口前移 {shift_days} 個交易日，提示需提前啟動防禦部署。"
        )
        adj_start_date = _shift_business_days(start_date, -shift_days)
        adj_end_date = _shift_business_days(end_date, -shift_days)

    elif direction == "後推":
        tactical_directive = (
            "🟢 **延後抱牢反彈**：寬鬆流動性預期與正 Gamma 結構護航，多頭動能延續性強，"
            "建議延後撤退時機、讓利潤奔馳，可適度提高風險偏好。"
        )
        reason = (
            f"宏觀流動性矩陣呈現寬鬆擴張格局（FedWatch 維持高利率機率僅 {prob * 100:.1f}%、通膨放緩且大盤結構健康）。"
            f"系統自動將自訂反彈逃頂窗口後推 {shift_days} 個交易日，建議延後多頭撤退時機、充分享受流動性溢價。"
        )
        adj_start_date = _shift_business_days(start_date, shift_days)
        adj_end_date = _shift_business_days(end_date, shift_days)

    else:
        tactical_directive = (
            "🟡 **正常波段護航**：各項宏觀流動性因子處於均衡區間，"
            "建議按原定計畫嚴守關鍵支撐與壓力關卡，執行標準網格防禦。"
        )
        reason = (
            f"當前 FedWatch 利率定價 ({prob * 100:.1f}%) 與各項總經流動性因子處於常態均衡區間。"
            "系統評估反彈逃頂窗口維持原訂日程 (偏移 0 天)，建議持續監控大盤結構變化。"
        )
        adj_start_date = start_date
        adj_end_date = end_date

    adjusted_start = f"{adj_start_date.month}月{_get_period_label(adj_start_date.day)} ({adj_start_date.strftime('%m-%d')})"
    adjusted_end = f"{adj_end_date.month}月{_get_period_label(adj_end_date.day)} ({adj_end_date.strftime('%m-%d')})"

    from cogs.embed_builder import create_fomc_escape_window_embed

    return create_fomc_escape_window_embed(
        prob=prob,
        direction=direction,
        shift_days=shift_days,
        adjusted_start=adjusted_start,
        adjusted_end=adjusted_end,
        reason=reason,
        is_fallback=is_fallback,
        tier_title=tier_title,
        tactical_directive=tactical_directive,
        factors_summary=factors_summary,
        was_auto_rolled=was_auto_rolled,
        original_window_label=orig_label,
    )
