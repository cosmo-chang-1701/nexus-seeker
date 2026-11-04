"""Next-day strategy and macro escape (liquidity state + top-escape score) runners."""

from __future__ import annotations
from typing import Any

import asyncio
import logging
import math
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


async def run_fomc_escape_window_analysis(
    user_id: int,
) -> Optional[discord.Embed]:
    """計算宏觀流動性狀態與逃頂評分，回傳盤前簡報 Embed。

    與進場閘門共用同一套輸入（get_market_regime / VTS / Fear & Greed / FedWatch）。
    """

    # 1. 取得 FedWatch 概率（同步 SQLite 讀取移出 event loop）
    # 過期時 prob=None、prob_stale=True（與進場閘門相同的 fail-closed 語意，簡報與
    # 閘門得出相同 tier）；例外時 prob=None（未知），不再冒充寫死的 0.50。
    prob: Optional[float]
    try:
        from services.calendar_service import calendar_service

        (
            prob,
            is_fallback,
            fedwatch_details,
            prob_stale,
        ) = await asyncio.to_thread(calendar_service.get_latest_fedwatch_info)
    except Exception as e:
        logger.warning(f"查詢 FedWatch 數據失敗: {e}")
        prob = None
        is_fallback = True
        fedwatch_details = {}
        prob_stale = False

    from database.cache import FEDWATCH_PROB_MAX_AGE_HOURS

    if prob is not None:
        prob_pct_str = f"{prob * 100:.1f}%"
    else:
        prob_pct_str = "資料過期" if prob_stale else "暫無數據"

    # 2. 載入使用者設定（僅用於決定逃頂指引是否提示口數推播；同步 SQLite 讀取移出 event loop）
    from database.user_settings import get_full_user_context

    ctx = await asyncio.to_thread(get_full_user_context, user_id)
    defense_enabled = bool(ctx is not None and ctx.enable_macro_top_escape_defense)

    # 3. 收集四大宏觀因子
    from database.cache import get_kv_cache

    factors_summary: list[tuple[str, str]] = []

    # Factor 1: FedWatch 利率定價
    meeting_date = fedwatch_details.get("meeting_date", "")
    meeting_prefix = f"({meeting_date}) " if meeting_date else ""
    prob_m = fedwatch_details.get("prob_maintain")
    prob_h = fedwatch_details.get("prob_hike")
    prob_c = fedwatch_details.get("prob_cut")
    decision = fedwatch_details.get("decision", "")

    if prob_m is not None or prob_h is not None or prob_c is not None:
        p_m = float(prob_m) if prob_m is not None else 0.0
        p_h = float(prob_h) if prob_h is not None else 0.0
        p_c = float(prob_c) if prob_c is not None else 0.0

        ladder_saturated = bool(fedwatch_details.get("ladder_saturated"))
        p_h50 = float(fedwatch_details.get("prob_hike_50") or 0.0)
        p_h25 = float(fedwatch_details.get("prob_hike_25") or 0.0)
        p_c50 = float(fedwatch_details.get("prob_cut_50") or 0.0)
        p_c25 = float(fedwatch_details.get("prob_cut_25") or 0.0)

        if p_h >= 1.0:
            hike_ladder = (
                f" (1碼 {p_h25:.1f}% / 2碼+ {p_h50:.1f}%)"
                if ladder_saturated and p_h50 > 0.0
                else ""
            )
            if p_h >= p_m:
                detail_str = (
                    f"加息 {p_h:.1f}%{hike_ladder} / 維持 {p_m:.1f}% / 降息 {p_c:.1f}%"
                )
            else:
                detail_str = (
                    f"維持 {p_m:.1f}% / 加息 {p_h:.1f}%{hike_ladder} / 降息 {p_c:.1f}%"
                )
        else:
            cut_ladder = (
                f" (1碼 {p_c25:.1f}% / 2碼+ {p_c50:.1f}%)"
                if ladder_saturated and p_c50 > 0.0
                else ""
            )
            if p_c >= p_m:
                detail_str = f"降息 {p_c:.1f}%{cut_ladder} / 維持 {p_m:.1f}%"
            else:
                detail_str = f"維持 {p_m:.1f}% / 降息 {p_c:.1f}%{cut_ladder}"

        if (
            p_h >= 50.0
            or decision == "hike"
            or (p_h >= 30.0 and p_h > p_c and p_h > p_m)
        ):
            f1_val = f"\u001b[1;31m🚨 {meeting_prefix}鷹派加息 ({detail_str})\u001b[0m"
        elif (
            p_c >= 50.0
            or decision == "cut"
            or (p_c >= 30.0 and p_c > p_h and p_c > p_m)
        ):
            f1_val = f"\u001b[1;32m🟢 {meeting_prefix}降息確立 ({detail_str})\u001b[0m"
        elif p_m >= 50.0 or decision == "maintain":
            f1_val = f"\u001b[1;33m🟡 {meeting_prefix}維持利率 ({detail_str})\u001b[0m"
        else:
            f1_val = f"\u001b[1;33m🟡 {meeting_prefix}均衡定價 ({detail_str})\u001b[0m"
    elif prob is None:
        f1_val = (
            f"\u001b[1;33m⚪ FedWatch 資料過期（逾 {FEDWATCH_PROB_MAX_AGE_HOURS} 小時），不計分\u001b[0m"
            if prob_stale
            else "⚪ 暫無數據"
        )
    else:
        if prob > 0.70:
            f1_val = (
                f"\u001b[1;31m🚨 {meeting_prefix}鷹派高位 ({prob * 100:.1f}%)\u001b[0m"
            )
        elif prob <= 0.40:
            f1_val = (
                f"\u001b[1;32m🟢 {meeting_prefix}降息確立 ({prob * 100:.1f}%)\u001b[0m"
            )
        else:
            f1_val = (
                f"\u001b[1;33m🟡 {meeting_prefix}均衡定價 ({prob * 100:.1f}%)\u001b[0m"
            )
    factors_summary.append(("FOMC 利率定價 (FedWatch)", f1_val))

    # Factor 2: 通膨與能源 (CPI / WTI)
    cpi_actual = get_kv_cache("macro_cpi_actual")
    cpi_expected = get_kv_cache("macro_cpi_expected")
    cpi_dev = (
        cpi_actual - cpi_expected
        if (cpi_actual is not None and cpi_expected is not None)
        else get_kv_cache("macro_cpi_deviation")
    )
    wti_raw = get_kv_cache("macro_wti")
    # 未知時不再補 WTI $75 / CPI 偏差 0 冒充「通膨平穩」
    wti = float(wti_raw) if wti_raw is not None and float(wti_raw) > 0 else None
    cpi_dev = float(cpi_dev) if cpi_dev is not None else None
    wti_str = f"${wti:.1f}" if wti is not None else "--"
    cpi_str = f"{cpi_dev:+.2f}%" if cpi_dev is not None else "--"
    f2_detail = f"(WTI {wti_str}, CPI偏差 {cpi_str})"

    if (cpi_dev is not None and cpi_dev > 0.1) or (wti is not None and wti > 85.0):
        f2_val = f"\u001b[1;31m🚨 通膨偏高 {f2_detail}\u001b[0m"
    elif cpi_dev is None or wti is None:
        f2_val = f"⚪ 數據不足 {f2_detail}"
    elif (cpi_dev <= 0.0) and (wti <= 80.0):
        f2_val = f"\u001b[1;32m🟢 通膨平穩 {f2_detail}\u001b[0m"
    else:
        f2_val = f"\u001b[1;33m🟡 通膨受控 {f2_detail}\u001b[0m"
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
        f3_val = "⚪ 數據未更新 (不計分)"
    elif vts_ratio >= 1.0:
        f3_val = f"\u001b[1;31m🚨 期限倒掛 (VTS: {vts_ratio:.3f})\u001b[0m"
    elif vts_ratio < 0.90:
        f3_val = f"\u001b[1;32m🟢 正價差健康 (VTS: {vts_ratio:.3f})\u001b[0m"
    else:
        f3_val = f"\u001b[1;33m🟡 期限正常 (VTS: {vts_ratio:.3f})\u001b[0m"
    factors_summary.append(("恐慌期限結構 (VIX Term)", f3_val))

    # Factor 4: 大盤負 Gamma（與進場閘門 compute_macro_escape_tier 同一套推導：
    # get_market_regime()；UNKNOWN 視為未知，不計分）
    is_negative_gamma: Optional[bool] = None
    try:
        from market_analysis.index_microstructure import get_market_regime

        regime = await get_market_regime()
        is_negative_gamma = (
            None
            if regime == "UNKNOWN"
            else regime in ("SHORT_GAMMA_CRITICAL", "SYSTEMIC_LIQUIDITY_CRISIS")
        )
    except Exception as e:
        logger.warning(f"取得大盤 Market Regime 失敗: {e}")
    if is_negative_gamma is None:
        f4_val = "⚪ 未知（不計分）"
    elif is_negative_gamma:
        f4_val = "\u001b[1;31m🚨 負 Gamma 踩踏\u001b[0m"
    else:
        f4_val = "\u001b[1;32m🟢 未達踩踏條件\u001b[0m"
    factors_summary.append(("大盤微觀結構 (SPY GEX)", f4_val))

    # 4. 三階矩陣狀態評估 (使用統一的宏觀流動性矩陣引擎)
    from market_analysis.index_microstructure import evaluate_escape_window_regime

    (
        tightening_score,
        easing_score,
        direction,
        tier_title,
        status_desc,
    ) = evaluate_escape_window_regime(
        prob=prob,
        cpi_dev=cpi_dev,
        wti=wti,
        vts_ratio=vts_ratio if is_vts_valid else None,
        is_negative_gamma=is_negative_gamma,
    )

    # 4.5. 宏觀逃頂綜合評分 (Top-Escape Composite Score) — 獨立於上方的利率擇時矩陣，
    # 疊加 Fear & Greed 極度貪婪訊號。沿用同一次 fetch_core_macro_metrics() 呼叫
    # (150 秒快取)，不新增任何額外抓取成本。第 5 個「衛星持倉亢奮廣度」因子刻意留白
    # (satellite_euphoria_ratio=None)：其即時計算已由 15 分鐘轉倉排程
    # (Dynamic Rollover Scenario 6) 在記憶體中完成，這裡不重複發起額外的 GEX/Skew
    # 抓取，避免拖慢每日一次的盤前簡報路徑。
    from market_analysis.index_microstructure import (
        evaluate_macro_top_escape_score,
        fetch_core_macro_metrics,
    )

    # 未知一律傳 None (不補 48 / 0.88)，評分卡會標示「資料不足」。
    fear_greed: Optional[float] = None
    try:
        core_metrics = await fetch_core_macro_metrics()
        fg_raw = core_metrics.get("fear_greed")
        if fg_raw is not None and not core_metrics.get("_is_fallback"):
            fear_greed = float(fg_raw)
    except Exception as e:
        logger.warning(f"評估宏觀逃頂綜合評分時取得 Fear & Greed 指數失敗: {e}")

    (
        top_escape_score,
        top_escape_tier_code,
        top_escape_tier_title,
        top_escape_factors,
    ) = evaluate_macro_top_escape_score(
        vts_ratio=vts_ratio if is_vts_valid else None,
        fear_greed=fear_greed,
        prob=prob,
        is_negative_gamma=is_negative_gamma,
        prob_stale=prob_stale,
    )

    # 逃頂指引：僅依逃頂評分分級；唯一動作是 SPY 保護性 Put（情境 6）
    put_hint = (
        "具體口數見盤中情境 6 推播"
        if defense_enabled
        else "可於 /settings 開啟「宏觀逃頂前瞻防禦」以收到口數建議"
    )
    if top_escape_tier_code == "NORMAL":
        tactical_directive = "🟢 **維持持有**：逃頂評分常態，無需額外避險。"
    elif top_escape_tier_code == "WATCH":
        tactical_directive = (
            "🟡 **前哨**：考慮 SPY 保護性 Put（情境 6，對沖約 30% Beta 加權 Delta，"
            f"保留上檔）；{put_hint}。"
        )
    elif top_escape_tier_code == "ELEVATED":
        tactical_directive = (
            "🟠 **警戒升高**：考慮 SPY 保護性 Put（情境 6，對沖約 30% Beta 加權 Delta，"
            f"保留上檔）；{put_hint}。"
        )
    elif top_escape_tier_code == "CRITICAL":
        tactical_directive = (
            "🔴 **確認級**：考慮 SPY 保護性 Put（情境 6，對沖約 30% Beta 加權 Delta，"
            f"保留上檔）；{put_hint}。"
        )
    else:
        tactical_directive = "⚪ 資料不足，無法判定逃頂分級。"

    gamma_label = (
        "未知（不計分）"
        if is_negative_gamma is None
        else ("負 Gamma 踩踏" if is_negative_gamma else "未達踩踏條件")
    )
    reason = (
        f"流動性狀態：{direction}（收縮 {tightening_score} 項／寬鬆 {easing_score} 項；{status_desc}）。"
        f"FedWatch 鷹派傾向分數 {prob_pct_str}，大盤 Gamma：{gamma_label}。"
        f"逃頂評分：{top_escape_tier_title}（綜合 {top_escape_score} 分）。"
    )

    from cogs.embed_builder import create_fomc_escape_window_embed

    return create_fomc_escape_window_embed(
        direction=direction,
        reason=reason,
        is_fallback=is_fallback,
        tier_title=tier_title,
        tactical_directive=tactical_directive,
        factors_summary=factors_summary,
        top_escape_score=top_escape_score,
        top_escape_tier=top_escape_tier_title,
        top_escape_factors=top_escape_factors,
        fedwatch_source=fedwatch_details.get("source"),
        prob_stale=prob_stale,
    )
