"""Next-day strategy and FOMC escape-window analysis runners."""

from __future__ import annotations

import logging
import math
import sqlite3
from datetime import datetime, timezone, timedelta
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


async def run_next_day_strategy(fetch_macro_fn) -> str:
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
        vts_ratio = vts_data.get("vts_ratio", 1.0)
        vts_state = vts_data.get("vts_state", "UNKNOWN")
        vix_front = vts_data.get("vix_front")
        vix_back = vts_data.get("vix_back")
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


async def run_fomc_escape_window_analysis(
    user_id: int,
) -> Optional[discord.Embed]:
    """Dynamically compute FOMC-adjusted escape window and return a styled Embed."""
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

    # 2. 載入使用者自訂逃頂窗口
    from database.user_settings import get_full_user_context

    ctx = get_full_user_context(user_id)
    start_setting = ctx.escape_window_start if ctx else "07-15"
    end_setting = ctx.escape_window_end if ctx else "07-31"

    try:
        start_m, start_d = map(int, start_setting.split("-"))
        end_m, end_d = map(int, end_setting.split("-"))
    except Exception:
        start_m, start_d = 7, 15
        end_m, end_d = 7, 31

    custom_period_label = (
        f"{start_m}月{_get_period_label(start_d)}至{end_m}月{_get_period_label(end_d)}"
    )

    # 3. 通膨 / 油價修飾因子
    from database.cache import get_kv_cache

    cpi_actual = get_kv_cache("macro_cpi_actual")
    cpi_expected = get_kv_cache("macro_cpi_expected")
    cpi_dev = (
        cpi_actual - cpi_expected
        if (cpi_actual is not None and cpi_expected is not None)
        else (get_kv_cache("macro_cpi_deviation") or 0.0)
    )
    wti = get_kv_cache("macro_wti") or 75.0
    is_inflation_high = (cpi_dev > 0.1) or (wti > 85.0)

    should_advance = is_inflation_high or (prob <= 0.70)
    shift_days = 5

    if should_advance:
        adj_start_d = start_d - shift_days
        adj_end_d = end_d - shift_days
        adj_start_m = start_m
        adj_end_m = end_m
        if adj_start_d <= 0:
            adj_start_d += 30
            adj_start_m = 12 if start_m == 1 else start_m - 1
        if adj_end_d <= 0:
            adj_end_d += 30
            adj_end_m = 12 if end_m == 1 else end_m - 1

        adjusted_start = f"{adj_start_m}月{_get_period_label(adj_start_d)} (約 {adj_start_m:02d}-{adj_start_d:02d})"
        adjusted_end = f"{adj_end_m}月{_get_period_label(adj_end_d)} (約 {adj_end_m:02d}-{adj_end_d:02d})"
        direction = "前移"
        if is_inflation_high:
            reason = (
                f"由於核心 CPI/PCE 高於預期 ({cpi_dev:+.2f}%) 或 WTI 油價達 ${wti:.1f} 觸及通膨高風險閾值，"
                f"通膨壓力上升可能導致政策收緊。系統自動將您自訂的 {custom_period_label} 反彈逃頂窗口前移 {shift_days} 個交易日，提示需提前防禦撤退。"
            )
        else:
            reason = (
                f"由於下週 FOMC 維持高利率/加息機率僅 {prob*100:.1f}%，小於 70% 臨界值，市場預期利空出盡。"
                f"系統自動將您自訂的 {custom_period_label} 反彈逃頂窗口前移 {shift_days} 個交易日，提示多頭反彈可能提前發酵，需作好提前撤退部署。"
            )
    else:
        adj_start_d = start_d + shift_days
        adj_end_d = end_d + shift_days
        adj_start_m = start_m
        adj_end_m = end_m
        if adj_start_d > 30:
            adj_start_d -= 30
            adj_start_m = 1 if start_m == 12 else start_m + 1
        if adj_end_d > 30:
            adj_end_d -= 30
            adj_end_m = 1 if end_m == 12 else end_m + 1

        adjusted_start = f"{adj_start_m}月{_get_period_label(adj_start_d)} (約 {adj_start_m:02d}-{adj_start_d:02d})"
        adjusted_end = f"{adj_end_m}月{_get_period_label(adj_end_d)} (約 {adj_end_m:02d}-{adj_end_d:02d})"
        direction = "後推"
        reason = (
            f"由於通膨與油價數據放緩 (WTI: ${wti:.1f}, CPI偏差: {cpi_dev:+.2f}%) 且下週 FOMC 維持高利率機率為 {prob*100:.1f}%，"
            f"市場流動性緊縮預期減弱。系統自動將您自訂的 {custom_period_label} 反彈逃頂窗口後推 {shift_days} 個交易日，建議延後多頭撤退計劃。"
        )

    from cogs.embed_builder import create_fomc_escape_window_embed

    return create_fomc_escape_window_embed(
        prob=prob,
        direction=direction,
        shift_days=shift_days,
        adjusted_start=adjusted_start,
        adjusted_end=adjusted_end,
        reason=reason,
        is_fallback=is_fallback,
    )
