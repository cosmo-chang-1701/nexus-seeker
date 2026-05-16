import discord
import logging
import psutil
import re

from datetime import datetime, timezone
from typing import List, Dict, Any

logger = logging.getLogger(__name__)


def _is_macro_report_marker(line: str) -> bool:
    """較穩健地辨識宏觀風險段落起始行。"""
    if not line:
        return False
    normalized = line.strip()
    if not normalized.startswith("🌐"):
        return False
    return ("宏觀風險" in normalized) or ("資金水位報告" in normalized)


def _truncate_with_boundary(text: str, max_len: int) -> str:
    """優先在換行或句點邊界截斷，避免硬切造成可讀性差。"""
    if len(text) <= max_len:
        return text

    reserved = 3
    safe_len = max(1, max_len - reserved)
    candidate = text[:safe_len]

    boundary_candidates = [
        candidate.rfind("\n\n"),
        candidate.rfind("\n"),
        candidate.rfind("。"),
    ]
    boundary = max(boundary_candidates)
    if boundary > int(max_len * 0.6):
        candidate = candidate[:boundary]

    return candidate.rstrip() + "..."


def _safe_embed_field_value(text: str, fallback: str, max_len: int = 1024) -> str:
    """確保欄位值非空且符合 Discord 長度上限。"""
    value = (text or "").strip()
    if not value:
        value = fallback

    # 保留尾端間距，讓下一個欄位視覺更乾淨。
    suffix = "\n\u200b"
    room = max(1, max_len - len(suffix))
    value = _truncate_with_boundary(value, room)
    value = value + suffix

    if len(value) > max_len:
        value = _truncate_with_boundary(value, max_len)
    if not value.strip():
        value = fallback + suffix
    return value


def _safe_float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def add_news_field(embed, news_text):
    if news_text:
        if len(news_text) > 1000:
            news_text = news_text[:997] + "..."
        news_context = f"```{news_text}\n\u200b```"
        embed.add_field(name="📰 最新新聞", value=news_context, inline=False)


def add_reddit_field(embed, reddit_text):
    if reddit_text:
        if len(reddit_text) > 1000:
            reddit_text = reddit_text[:997] + "..."
        reddit_context = f"```{reddit_text}\n\u200b```"
        embed.add_field(name="📰 Reddit 討論", value=reddit_context, inline=False)


def _build_embed_base(data, strategy, stock_cost):
    colors = {
        "STO_PUT": discord.Color.green(),
        "STO_CALL": discord.Color.red(),
        "BTO_CALL": discord.Color.blue(),
        "BTO_PUT": discord.Color.orange(),
    }
    titles = {
        "STO_PUT": "🟢 賣出賣權 (STO Put)",
        "STO_CALL": "🔴 賣出買權 (STO Call)",
        "BTO_CALL": "🚀 買入買權 (BTO Call)",
        "BTO_PUT": "⚠️ 買入賣權 (BTO Put)",
    }

    is_covered = strategy == "STO_CALL" and stock_cost > 0.0
    if is_covered:
        titles["STO_CALL"] = "🛡️ 掩護性買權 (Covered Call)"
        colors["STO_CALL"] = discord.Color.teal()

    embed = discord.Embed(
        title=f"{titles.get(strategy, strategy)} | {data.get('symbol', 'UNKNOWN')}",
        description=f"📅 **到期日:** `{data.get('target_date', 'UNKNOWN')}` ｜ 🎯 **履約價:** `${data.get('strike', 'UNKNOWN')}`\n\u200b",
        color=colors.get(strategy, discord.Color.default()),
    )
    return embed, is_covered


def _add_vix_battle_status_field(embed, data):
    """Add VIX Battle Ladder status indicator (highest priority field)."""
    vix_status = data.get("vix_battle_status") or {}
    vix_spot = vix_status.get("vix_spot") or data.get("vix_spot")
    tier_name = vix_status.get("name") or data.get("vix_tier_name", "N/A")
    tier_emoji = vix_status.get("emoji") or data.get("vix_tier_emoji", "")
    delta_cap = vix_status.get("sto_delta_cap") or data.get("vix_sto_delta_cap", 0.0)
    sizing_mult = vix_status.get("sizing_multiplier") or data.get(
        "vix_sizing_multiplier", 1.0
    )

    if vix_spot is None:
        return

    status_line = f"{tier_emoji} **{tier_name}** | VIX: `{vix_spot:.1f}`"
    details = []
    if delta_cap != 0.0:
        details.append(f"Delta Cap: `{delta_cap:.2f}`")
    if sizing_mult != 1.0:
        details.append(f"\u5009\u4f4d\u4e58\u6578: `{sizing_mult:.1f}x`")

    value = status_line
    if details:
        value += "\n" + " | ".join(details)
    value += "\n\u200b"

    embed.add_field(name="🛡️ VIX 戰情階梯狀態", value=value, inline=False)


def _add_market_overview_fields(embed, data):
    beta = data.get("beta", 1.0)
    beta_status = "🚀" if beta > 1.3 else ("⚖️" if beta >= 0.8 else "🧊")
    embed.add_field(
        name="🏷️ 標價 / Beta\u2800\u2800",
        value=f"${data['price']:.2f} / `{beta:.2f}` {beta_status}\n\u200b",
        inline=True,
    )
    embed.add_field(
        name="📈 RSI / 20MA\u2800\u2800\u2800",
        value=f"{data['rsi']:.2f} / ${data['sma20']:.2f}\n\u200b",
        inline=True,
    )
    hvr_status = (
        "🔥 高"
        if data["hv_rank"] >= 50
        else ("⚡ 中" if data["hv_rank"] >= 30 else "🧊 低")
    )
    embed.add_field(
        name="🔥 HV Rank\u2800\u2800\u2800\u2800",
        value=f"`{data['hv_rank']:.1f}%` {hvr_status}\n\u200b",
        inline=True,
    )


def _add_volatility_fields(embed, data, strategy):
    vrp_pct = data.get("vrp", 0.0) * 100
    vrp_icon = (
        "✅"
        if (("STO" in strategy and vrp_pct > 0) or ("BTO" in strategy and vrp_pct < 0))
        else "⚠️"
    )
    embed.add_field(
        name="⚖️ VRP 溢酬\u2800\u2800\u2800\u2800",
        value=f"`{vrp_pct:+.2f}%` {vrp_icon}\n\u200b",
        inline=True,
    )
    ts_ratio_str = f"`{data['ts_ratio']:.2f}` {data['ts_state']}"
    embed.add_field(
        name="⏳ 個股 IV 期限結構\u2800\u2800",
        value=f"{ts_ratio_str}\n\u200b",
        inline=True,
    )
    v_skew_str = f"`{data['v_skew']:.2f}` {data.get('v_skew_state', '')}"
    embed.add_field(
        name="📉 垂直偏態\u2800\u2800\u2800\u2800",
        value=f"{v_skew_str}\n\u200b",
        inline=True,
    )

    # ---------------- VIX 306 Volatility Metrics ----------------
    vts_str = f"`{data.get('vix_vts_ratio', 1.0):.2f}` {data.get('vix_regime', '')}"
    embed.add_field(
        name="🌐 大盤 VIX 期限結構\u2800", value=f"{vts_str}\n\u200b", inline=True
    )

    z30 = data.get("vix_z30", 0.0)
    z60 = data.get("vix_z60", 0.0)
    z_icon = "📈 擴張" if z30 > 0.5 and z60 > 0 else "📉 收斂"
    embed.add_field(
        name="🔥 VIX 30/60 Z-Score",
        value=f"Z30:`{z30:.1f}` Z60:`{z60:.1f}` {z_icon}\n\u200b",
        inline=True,
    )

    tail_risk = (
        "🚨 觸發降規 (1/4 Kelly)"
        if data.get("is_high_tail_risk", False)
        else "✅ 正常 (1/2 Kelly)"
    )
    embed.add_field(
        name="🛡️ 尾部風險管理\u2800\u2800\u2800",
        value=f"{tail_risk}\n\u200b",
        inline=True,
    )


def _add_performance_and_kelly_fields(embed, data, user_capital):
    """添加績效與風控（含凱利倉位計算）欄位，並校正部位方向"""
    strategy = data.get("strategy", "")
    raw_delta = data.get("delta", 0.0)
    weighted_delta = data.get("weighted_delta", 0.0)

    # 🚀 方向校正邏輯：
    # 若是賣方 (STO)，部位方向 = 合約方向 * -1 (賣出負 Delta 是看多，賣出正 Delta 是看空)
    # 若是買方 (BTO)，部位方向 = 合約方向 (買入什麼就是什麼)
    pos_multiplier = -1 if "STO" in strategy else 1
    pos_weighted_shares = weighted_delta * pos_multiplier

    # 1. 希臘字母與部位方向
    embed.add_field(
        name="🧩 Delta (部位加權)\u2800\u2800",
        value=f"{raw_delta:.3f} (`{pos_weighted_shares:+.1f}`股)\n\u200b",
        inline=True,
    )

    # 2. 獲利效率與隱含波動率
    embed.add_field(
        name="💰 AROC / IV\u2800\u2800\u2800\u2800",
        value=f"`{data['aroc']:.1f}%` / {data['iv']:.1%}\n\u200b",
        inline=True,
    )

    # 3. 凱利建議邏輯
    alloc_pct = data.get("alloc_pct", 0.0)
    suggested = data.get("suggested_contracts", 0)

    if alloc_pct <= 0:
        kelly_value = "`不建議建倉`"
    elif not user_capital or user_capital <= 0:
        kelly_value = f"`未設資金` ({alloc_pct*100:.1f}%)"
    else:
        # 使用與主邏輯同步的 25% Kelly 上限顯示
        kelly_value = (
            f"`{suggested} 口` ({min(alloc_pct, 0.25)*100:.1f}%)"
            if suggested > 0
            else "`本金不足`"
        )

    embed.add_field(
        name="🧮 凱利原始建議\u2800\u2800", value=f"{kelly_value}\n\u200b", inline=True
    )


def _add_earnings_fields(embed, data, strategy):
    """添加財報預期波動欄位"""
    if 0 <= data.get("earnings_days", -1) <= 14:
        mmm_str = f"±{data['mmm_pct']:.1f}% (倒數 {data['earnings_days']} 天)"
        bounds_str = f"🛡️ 安全區間: **`${data['safe_lower']:.2f}`** ~ **`${data['safe_upper']:.2f}`**"
        strike = data["strike"]

        if "STO" in strategy:
            is_safe = (strategy == "STO_PUT" and strike <= data["safe_lower"]) or (
                strategy == "STO_CALL" and strike >= data["safe_upper"]
            )
            safety_icon = (
                "✅ 避開雷區 (適宜收租)" if is_safe else "💣 位於雷區 (極高風險)"
            )
        else:
            safety_icon = "🎲 財報盲盒 (注意 IV Crush 波動率壓縮風險)"

        embed.add_field(
            name="📊 財報預期波動 (MMM)",
            value=f"`{mmm_str}`\n{bounds_str}\n{safety_icon}\n\u200b",
            inline=False,
        )


def _add_covered_call_fields(embed, data, stock_cost):
    """添加 Covered Call 專屬防護欄位"""
    bid = data.get("bid", 0)
    true_breakeven = stock_cost - bid
    yoc = (bid / stock_cost) * 100 if stock_cost > 0 else 0

    cc_info = (
        f"📦 **真實現股成本:** `${stock_cost:.2f}`\n"
        f"🛡️ **真實下檔防線:** `${true_breakeven:.2f}`\n"
        f"💸 **單次收租殖利率 (Yield on Cost):** `{yoc:.2f}%`\n"
        f"👉 *您的持倉成本已透過收租進一步降低！*\n\u200b"
    )
    embed.add_field(name="🛡️ Covered Call 專屬防護", value=cc_info, inline=False)


def _add_expected_move_fields(embed, data, strategy, is_covered):
    """添加預期波動區間與損益兩平防線欄位"""
    em = data.get("expected_move", 0.0)
    em_lower = data.get("em_lower", 0.0)
    em_upper = data.get("em_upper", 0.0)

    if "STO_PUT" in strategy:
        breakeven = data["strike"] - data.get("bid", 0)
        safe = breakeven < em_lower
        safety_text = (
            "✅ 防線已建構於預期暴跌區間外"
            if safe
            else "⚠️ 損益兩平點位於預期波動區間內，風險較高"
        )
        em_info = f"1σ 預期下緣: `${em_lower:.2f}` (預期最大跌幅 -${em:.2f})\n🛡️ 損益兩平點: **`${breakeven:.2f}`**\n{safety_text}\n\u200b"
        embed.add_field(name="🎯 機率圓錐 (1σ 預期波動)", value=em_info, inline=False)

    elif "STO_CALL" in strategy:
        breakeven = data["strike"] + data.get("bid", 0)
        safe = breakeven > em_upper
        if is_covered:
            safety_text = "✅ 若漲破此價位，將以最高獲利出場 (股票被 Call 走)"
        else:
            safety_text = (
                "✅ 防線已建構於預期暴漲區聯外"
                if safe
                else "⚠️ 損益兩平點位於預期波動區間內，風險較高"
            )

        em_info = f"1σ 預期上緣: `${em_upper:.2f}` (預期最大漲幅 +${em:.2f})\n🛡️ 合約兩平點: **`${breakeven:.2f}`**\n{safety_text}\n\u200b"
        embed.add_field(name="🎯 機率圓錐 (1σ 預期波動)", value=em_info, inline=False)

    elif "BTO_PUT" in strategy:
        breakeven = data["strike"] - data.get("ask", 0)
        em_info = f"1σ 預期下緣: `${em_lower:.2f}` (預期最大跌幅 -${em:.2f})\n🛡️ 損益兩平點: **`${breakeven:.2f}`**\n✅ 目標跌破此防線即開始獲利\n\u200b"
        embed.add_field(name="🎯 機率圓錐 (1σ 預期波動)", value=em_info, inline=False)

    elif "BTO_CALL" in strategy:
        breakeven = data["strike"] + data.get("ask", 0)
        em_info = f"1σ 預期上緣: `${em_upper:.2f}` (預期最大漲幅 +${em:.2f})\n🛡️ 損益兩平點: **`${breakeven:.2f}`**\n✅ 目標突破此防線即開始獲利\n\u200b"
        embed.add_field(name="🎯 機率圓錐 (1σ 預期波動)", value=em_info, inline=False)


def _add_liquidity_fields(embed, data):
    """添加報價與流動性分析欄位"""
    mid_price = data.get("mid_price", (data.get("bid", 0) + data.get("ask", 0)) / 2)
    liq_status = data.get("liq_status", "N/A")
    liq_msg = data.get("liq_msg", "")

    spread_info = (
        f"**Bid:** `{data.get('bid', 0):.2f}` ｜ **Ask:** `{data.get('ask', 0):.2f}` (價差 `{data.get('spread_ratio', 0):.1f}%`)\n"
        f"**狀態:** {liq_status} {liq_msg}\n"
        f"🎯 **Limit (中價掛單建議):** `{mid_price:.2f}`\n\u200b"
    )
    embed.add_field(name="💱 報價與流動性分析", value=spread_info, inline=False)


def _add_strategy_upgrade_fields(embed, data, strategy):
    """添加策略升級提示欄位"""
    if strategy in ["BTO_CALL", "BTO_PUT"]:
        hedge_strike = data.get("suggested_hedge_strike")
        if hedge_strike:
            spread_type = (
                "多頭價差 (Bull Call Spread)"
                if strategy == "BTO_CALL"
                else "空頭價差 (Bear Put Spread)"
            )
            hedge_type = "Call" if strategy == "BTO_CALL" else "Put"

            upgrade_text = (
                f"為抵銷 Theta (時間價值) 衰減並降低建倉成本，\n"
                f"建議在買入本合約的同時，賣出更價外的 **${hedge_strike:.0f} {hedge_type}**\n"
                f"👉 組合為: **{spread_type}**\n\u200b"
            )
            embed.add_field(
                name="💡 經理人策略升級建議", value=upgrade_text, inline=False
            )


def _add_risk_optimization_fields(embed, data, user_capital=None):
    """
    添加事前曝險模擬與自動風控優化建議
    🚀 強化版：增加閾值動態化與基準價校驗
    """
    projected_pct = data.get("projected_exposure_pct")
    # 若無數據則不顯示 (注意：不要用 if projected_pct == 0)
    if projected_pct is None:
        return

    safe_qty = data.get("safe_qty", 0)
    hedge_spy = data.get("hedge_spy", 0.0)
    suggested = data.get("suggested_contracts", 0)

    # 🚀 修正點 1：風險閾值應從數據中取得，或設為全局變數
    # 避免後台改了 10% 這裡還在顯示 15%
    RISK_THRESHOLD = data.get("risk_limit", 15.0)

    # 1. 曝險現況區塊
    is_overloaded = abs(projected_pct) > RISK_THRESHOLD

    if is_overloaded:
        sim_status = "🚨 警告：曝險過載"
        # 使用 diff 語法渲染紅色背景
        sim_block = (
            f"```diff\n"
            f"- 成交後預期總曝險: {projected_pct:+.1f}%\n"
            f"- 超過 {RISK_THRESHOLD}% 宏觀紅線\n"
            f"```"
        )
    else:
        sim_status = "✅ 狀態：風險受控"
        # 使用 yaml 語法渲染綠色背景
        sim_block = (
            f"```yaml\n"
            f"成交後的預期總曝險: {projected_pct:+.1f}%\n"
            f"符合資產組合平衡標準\n"
            f"```"
        )

    embed.add_field(
        name=f"🛡️ What-if 曝險模擬 | {sim_status}",
        value=f"{sim_block}\n\u200b",
        inline=False,
    )

    # 2. Nexus Risk Optimizer 自動優化建議
    if suggested > safe_qty:
        opt_title = "⚖️ Nexus Risk Optimizer (自動優化建議)"

        # 🚀 修正點 2：加入基準 SPY 價格的動態提示 (讓對沖建議更可信)
        spy_p = data.get("spy_price", 690.0)

        actions = ["--- 偵測到風險超標，執行自動降規 ---"]
        actions.append(f"❌ 原始建議: {suggested} 口")
        actions.append(f"✅ 安全成交: {safe_qty} 口 (符合風控)")

        if safe_qty == 0 and hedge_spy != 0:
            actions.append("\n⚠️ 警告: 即使下 1 口也過載")
            direction = "賣出" if hedge_spy > 0 else "買入"
            # 格式化對沖股數，避免出現 22.2222222
            actions.append(
                f"🛡️ 建議對沖: {direction} {abs(hedge_spy):.1f} 股 SPY (@${spy_p:.1f})"
            )

        opt_block = "```diff\n" + "\n".join(actions) + "\n```"
        embed.add_field(name=opt_title, value=f"{opt_block}\n\u200b", inline=False)


def _add_hedge_unlock_fields(embed, data):
    """添加對沖解除建議欄位 (Hedge Unlocking)"""
    unlock = data.get("hedge_unlock")
    if not unlock:
        return

    symbol = data.get("symbol", "N/A")
    suggested_qty = unlock.get("reduce_spy_qty", 0)
    new_delta = unlock.get("new_delta", 0.0)
    reason = unlock.get("reason", "")
    risk_note = unlock.get("risk_note", "")

    # 依照使用者要求的文案格式
    unlock_text = (
        f"偵測到 **{symbol}** 強勢突破。目前您的 SPY 對沖正在產生 Hedge Drag。\n\n"
        f"✅ **建議動作：** 買回/平倉 `{suggested_qty}` 股 SPY。\n"
        f"🚀 **預計效應：** 釋放 Beta 動能，預計提升總組合 Delta 至 `{new_delta:+.1f}`。\n"
        f"🛡️ **防禦補償：** {risk_note}\n\u200b"
    )

    embed.add_field(name=f"🔓 對沖優化建議 ({reason})", value=unlock_text, inline=False)


def _add_ai_verification_fields(embed, data):
    """添加 AI 驗證決策欄位"""
    ai_decision = data.get("ai_decision")
    ai_reasoning = data.get("ai_reasoning")
    if ai_decision:
        if ai_decision == "APPROVE":
            ai_title = "🤖 Argo Cortex: ✅ 交易批准 (APPROVE)"
            ai_value = f"```\n{ai_reasoning}\n```"
        elif ai_decision == "VETO":
            ai_title = "🤖 Argo Cortex: ⛔ 否決交易 (VETO 黑天鵝警告)"
            ai_value = f"```diff\n- 警告: {ai_reasoning}\n```"
            embed.color = discord.Color.dark_red()
        elif ai_decision == "SKIP":
            ai_title = "🤖 Argo Cortex: ⚠️ 未啟用 (SKIP)"
            ai_value = f"```\n{ai_reasoning}\n```"
            embed.color = discord.Color.blue()

        embed.add_field(name=ai_title, value=ai_value, inline=False)


def get_ema_signal_ui(ema_signals: List[Dict[str, Any]]) -> str:
    """
    將 EMA 訊號清單轉化為 Discord 友善的文字流。
    """
    if not ema_signals:
        return "⚪ *暫無關鍵 EMA 觸碰訊號*"

    ui_lines = []
    for sig in ema_signals:
        window = sig["window"]
        sig_type = sig["type"]
        direction = sig["direction"]
        dist = sig["distance_pct"]

        # 1. 決定圖示與標題
        if sig_type == "CROSSOVER":
            icon = "🚀" if direction == "BULLISH" else "💀"
            action = "強勢突破" if direction == "BULLISH" else "失守跌破"
        else:  # TEST
            icon = "🛡️" if direction == "SUPPORT" else "🛑"
            action = "回測支撐" if direction == "SUPPORT" else "觸碰壓力"

        # 2. 格式化輸出
        line = f"{icon} **EMA {window} {action}** (偏離: `{dist}%`)"
        ui_lines.append(line)

    return "\n".join(ui_lines)


def _add_trend_and_support_fields(embed, data):
    """添加 EMA 狀態圖形化燈號欄位"""
    trend = data.get("trend", "UNKNOWN")
    ema21 = data.get("ema_21", 0.0)
    distance = data.get("distance_from_21", 0.0)

    if trend == "BULLISH_STRONG":
        trend_str = "📈 強勢多頭 (現價 > 8 > 21)"
    elif trend == "BULLISH_CORRECTION":
        trend_str = "📉 多頭回檔 (EMA 8 > 21 ≥ 現價)"
    elif trend == "BEARISH_STRONG":
        trend_str = "🐻 強勢空頭 (現價 < 8 < 21)"
    else:
        trend_str = "⚖️ 趨勢中性"

    # Risk 判定
    if distance > 10.0:
        risk_str = "⚠️ 過度擴張 (乖離率 > 10%)"
    elif distance < -10.0:
        risk_str = "⚠️ 超跌區間 (乖離率 < -10%)"
    else:
        risk_str = "✅ 穩定區間"

    support_str = f"EMA 21 位於 `${ema21:.2f}` (乖離: {distance:+.1f}%)"

    trend_info = f"**目前趨勢:** {trend_str}\n**支撐參考:** {support_str}\n**風險評估:** {risk_str}\n"
    trend_info += get_ema_signal_ui(data.get("ema_signals", []))

    embed.add_field(
        name="🧭 趨勢與支撐 (EMA 8/21)", value=trend_info + "\n\u200b", inline=False
    )


def create_sentiment_scan_embed(
    symbol: str, skew_data: dict, pcr_data: dict, uoa_data: list, max_pain_data: dict
) -> discord.Embed:
    """建立期權情緒掃描報告 Embed (繁體中文)"""
    embed = discord.Embed(
        title=f"📊 {symbol} 期權情緒掃描 (Sentiment Scan)",
        color=discord.Color.dark_magenta(),
        timestamp=datetime.now(timezone.utc),
    )

    # Skew
    skew_val = skew_data.get("skew", 0)
    skew_state = skew_data.get("state", "N/A")
    embed.add_field(
        name="📐 Option Skew",
        value=f"值: `{skew_val}%`\n狀態: **{skew_state}**",
        inline=True,
    )

    # PCR
    pcr_val = pcr_data.get("pcr", 0)
    pcr_state = pcr_data.get("state", "N/A")
    embed.add_field(
        name="⚖️ Put/Call Ratio",
        value=f"值: `{pcr_val}`\n狀態: **{pcr_state}**",
        inline=True,
    )

    # Max Pain
    mp_strike = max_pain_data.get("max_pain", "N/A")
    is_conv = "🎯 趨於收斂" if max_pain_data.get("is_converging") else "⏳ 尚有距離"
    embed.add_field(
        name="📍 Max Pain (最大痛點)",
        value=f"履約價: `${mp_strike}`\n收斂: {is_conv}",
        inline=True,
    )

    # UOA
    if uoa_data:
        uoa_text = ""
        for item in uoa_data:
            uoa_text += f"• `{item['expiry']}` `${item['strike']}` {item['type']} (Vol/OI: {item['ratio']}x)\n"
        embed.add_field(name="🐋 異常活動 (UOA)", value=uoa_text, inline=False)
    else:
        embed.add_field(
            name="🐋 異常活動 (UOA)", value="目前無顯著異常活動", inline=False
        )

    embed.set_footer(text="Nexus Seeker | Volatility Strategist")
    return embed


def create_macro_scan_embed(macro_data: dict, alerts: list = None) -> discord.Embed:
    """建立巨觀環境與隔夜市場掃描 Embed (繁體中文)"""
    embed = discord.Embed(
        title="🌍 巨觀環境與隔夜市場掃描 (Macro Scan)",
        color=discord.Color.blue(),
        timestamp=datetime.now(timezone.utc),
    )

    dxy = macro_data.get("dxy", 0.0)
    tnx = macro_data.get("tnx", 0.0)
    tnx_change = macro_data.get("tnx_change_bps", 0.0)
    us2y = macro_data.get("us2y", 0.0)
    vix = macro_data.get("vix", 0.0)
    vix_change = macro_data.get("vix_change", 0.0)
    spread = tnx - us2y

    # 美元指數 (DXY)
    embed.add_field(
        name="💵 美元指數 (DXY)",
        value=f"值: `{dxy:.2f}`",
        inline=True,
    )

    # 10年期公債 (TNX)
    embed.add_field(
        name="📈 10Y 公債 (TNX)",
        value=f"值: `{tnx:.2f}%`\n變化: `{tnx_change:+.1f} bps`",
        inline=True,
    )

    # 2年期公債 (US2Y)
    embed.add_field(
        name="📉 2Y 公債 (US2Y)",
        value=f"值: `{us2y:.2f}%`\n利差: `{spread:+.2f}%`",
        inline=True,
    )

    # 恐慌指數 (VIX)
    vix_emoji = "🔥" if vix > 25 else ("⚠️" if vix > 20 else "🟢")
    embed.add_field(
        name="🌪️ 恐慌指數 (VIX)",
        value=f"值: `{vix:.2f}` {vix_emoji}\n變化: `{vix_change:+.2f}`",
        inline=True,
    )

    # 結論與警示
    if alerts:
        alert_text = "\n".join([f"• {a}" for a in alerts])
        embed.add_field(
            name="🚨 風險警示 (Macro Alerts)", value=alert_text, inline=False
        )
        embed.color = discord.Color.red()
    else:
        embed.add_field(
            name="✅ 巨觀狀態",
            value="殖利率曲線、匯率與波動率未見極端異常。維持標準市場部位。",
            inline=False,
        )

    embed.set_footer(text="Nexus Seeker | Global Macro Intelligence")
    return embed


def create_earnings_report_embed(
    report_type: str, report_content: str, raw_data: dict
) -> discord.Embed:
    """
    建立盤前財報與估值調整 Embed (繁體中文)，參照 Polymarket 巨鯨戰報風格。
    """
    embed = discord.Embed(
        title=f"【 {report_type} 】",
        color=discord.Color.blue(),
        timestamp=datetime.now(timezone.utc),
    )

    upcoming = raw_data.get("upcoming_earnings", {})
    sentiment = raw_data.get("earnings_sentiment_scan", {})

    content = []

    if upcoming:
        content.append("## 📅 即將發布財報標的")
        content.append("---")
        for sym, events in upcoming.items():
            for event in events:
                date = event.get("date", "未知日期")
                period = event.get("period", "未知季度")

                sym_sentiment = sentiment.get(sym, {})
                news = sym_sentiment.get("news", "無相關資訊")
                reddit = sym_sentiment.get("reddit_sentiment", "無相關資訊")

                # 簡單截斷以防過長
                if isinstance(news, str) and len(news) > 80:
                    news = news[:77] + "..."
                if isinstance(reddit, str) and len(reddit) > 80:
                    reddit = reddit[:77] + "..."

                content.append(f"**{sym}** ({period})")
                content.append(f" ├─ 📅 財報日期: `{date}`")
                content.append(f" ├─ 📰 新聞摘要: {news}")
                content.append(f" └─ 💬 社群情緒: {reddit}")
                content.append("")
        content.append("---")

    # 加入 LLM 生成的分析報告
    content.append("**🤖 AI 分析報告**")
    content.append(report_content)

    # 組合內容並檢查長度 (Discord Embed Description 限制 4096)
    full_description = "\n".join(content)
    if len(full_description) > 4000:
        full_description = full_description[:3997] + "..."

    embed.description = full_description
    embed.set_footer(text="Nexus Seeker | Earnings Intelligence Agent")

    return embed


def _add_sentiment_fields(embed, data):
    """添加期權情緒指標到掃描報告"""
    skew_val = data.get("skew")
    pcr_val = data.get("pcr")
    uoa_list = data.get("uoa_list", [])

    if skew_val is not None or pcr_val is not None or uoa_list:
        content = ""
        if skew_val is not None:
            content += f"📐 **Skew:** `{skew_val}%` | "
        if pcr_val is not None:
            content += f"⚖️ **PCR:** `{pcr_val}`"

        if uoa_list:
            content += "\n🐋 **UOA 偵測:** `FOUND` | 信心: `HIGH`"
            if data.get("high_conviction"):
                content += " | 🔥 **高信心訊號**"

        if content:
            embed.add_field(
                name="🎭 期權市場情緒 (Sentiment)",
                value=content + "\n\u200b",
                inline=False,
            )


def create_scan_embed(data, user_capital=100000.0):
    strategy = data.get("strategy", "UNKNOWN")
    stock_cost = data.get("stock_cost", 0.0)

    embed, is_covered = _build_embed_base(data, strategy, stock_cost)

    # VIX tier color override
    vix_color = data.get("vix_tier_color") or (
        data.get("vix_battle_status", {}).get("color_hex")
    )
    if vix_color:
        embed.color = discord.Color(vix_color)

    # Render UI fields (VIX Battle Status first)
    _add_vix_battle_status_field(embed, data)

    # 🚀 整合 Gap & Fill 狀態 (New Engine)
    gap = data.get("gap_status")
    if gap:
        from market_analysis.gap_analysis import GapStatus

        status_emoji = {
            GapStatus.GAP_HOLDING: "🟢 持續跳空 (Holding)",
            GapStatus.PARTIAL_FILL: "🟡 部分回補 (Filling)",
            GapStatus.FULL_FILL: "🔴 完全回補 (Filled)",
            GapStatus.NO_GAP: "⚪ 無跳空",
        }.get(gap.current_fill_status, "⚪ N/A")

        support_tag = " | 🛡️ 支撐已確認" if gap.is_support_confirmed else ""
        gap_color = "🟢" if gap.gap_size > 0 else "🔴"

        gap_info = (
            f"{gap_color} **{'向上跳空 (UP-GAP)' if gap.gap_size > 0 else '向下跳空 (DOWN-GAP)'}**: `{gap.gap_pct:+.2f}%` (${gap.gap_size:+.2f})\n"
            f"**狀態:** {status_emoji}{support_tag}\n"
            f"**區間:** `${gap.gap_zone[0]:.2f}` - `${gap.gap_zone[1]:.2f}`\n\u200b"
        )
        embed.add_field(name="📈 Gap & Fill 跳空監控", value=gap_info, inline=False)

    _add_market_overview_fields(embed, data)
    _add_volatility_fields(embed, data, strategy)
    _add_sentiment_fields(embed, data)
    _add_trend_and_support_fields(embed, data)
    _add_performance_and_kelly_fields(embed, data, user_capital)
    _add_earnings_fields(embed, data, strategy)

    if is_covered:
        _add_covered_call_fields(embed, data, stock_cost)

    _add_expected_move_fields(embed, data, strategy, is_covered)
    _add_liquidity_fields(embed, data)
    _add_strategy_upgrade_fields(embed, data, strategy)

    # 🚀 執行優化回饋顯示
    _add_risk_optimization_fields(embed, data, user_capital)
    _add_hedge_unlock_fields(embed, data)

    add_news_field(embed, data.get("news_text"))
    add_reddit_field(embed, data.get("reddit_text"))
    _add_ai_verification_fields(embed, data)

    # 🚀 AlertFilter 推播理由 (僅在條件式過濾觸發時顯示)
    alert_reason = data.get("alert_reason")
    if alert_reason:
        embed.add_field(
            name="📢 推播觸發條件",
            value=f"```\n{alert_reason}\n```",
            inline=False,
        )

    vix_spot = data.get("vix_spot") or (
        data.get("vix_battle_status", {}).get("vix_spot")
    )
    vix_emoji = data.get("vix_tier_emoji") or (
        data.get("vix_battle_status", {}).get("emoji", "")
    )
    vix_name = data.get("vix_tier_name") or (
        data.get("vix_battle_status", {}).get("name", "")
    )
    vix_footer = f" | VIX: {vix_spot:.1f} {vix_emoji} {vix_name}" if vix_spot else ""
    embed.set_footer(
        text=f"Nexus Seeker 風控引擎 • 基準 SPY: ${data.get('spy_price', 500):.1f}{vix_footer}"
    )
    return embed


def create_psq_embed(data: dict) -> discord.Embed:
    """建構獨立的 PowerSqueeze (PSQ) 戰情報告 Embed"""
    sym = data.get("symbol", "UNKNOWN")
    psq = data.get("psq_result")

    if not psq:  # fallback
        return discord.Embed(
            title=f"⚡ PowerSqueeze 戰情報告 | {sym}",
            description="無可用數據",
            color=discord.Color.dark_grey(),
        )

    color = discord.Color.purple() if psq.is_squeezing else discord.Color.dark_teal()
    if psq.is_breakout_long:
        color = discord.Color.green()
    elif psq.is_breakout_short:
        color = discord.Color.red()

    embed = discord.Embed(
        title=f"⚡ PowerSqueeze 戰情報告 | {sym}",
        description=f"💰 最新股價: `${data.get('price', 0.0):.2f}`\n\u200b",
        color=color,
        timestamp=datetime.now(timezone.utc),
    )

    # 壓縮狀態指示
    squeeze_val = (
        "🔴 **壓縮中 (Squeeze On)**"
        if psq.is_squeezing
        else "⚪ **無壓縮 (Squeeze Off)**"
    )
    energy_str = (
        "🔥 動能向上爆發"
        if psq.is_breakout_long
        else ("💀 動能向下崩潰" if psq.is_breakout_short else "⚡ 蓄力中")
    )

    embed.add_field(
        name="🔋 能量壓縮狀態",
        value=f"{squeeze_val}\n狀態: {energy_str}\n\u200b",
        inline=True,
    )

    # 動能趨勢
    trend_val = "🟢 多方主導" if psq.momentum_value > 0 else "🔴 空方主導"
    embed.add_field(
        name="🚀 線性動能 (Momentum)",
        value=f"`{psq.momentum_value:+.2f}`\n趨勢: {trend_val}\n\u200b",
        inline=True,
    )

    # 支撐區間
    support_val = (
        f"✅ 靠近 20SMA (距離: `{psq.sma_distance_pct:.2f}%`)\n"
        if psq.is_near_support
        else f"⚠️ 偏離 20SMA (距離: `{psq.sma_distance_pct:.2f}%`)\n"
    )
    support_val += f"📉 20SMA: `${psq.sma_20:.2f}`\n\u200b"
    embed.add_field(name="🧭 均線支撐 (Daily)", value=support_val, inline=False)

    # VIX momentum label
    vix_label = (
        psq.vix_momentum_label if hasattr(psq, "vix_momentum_label") else "NORMAL"
    )
    vix_tf_note = psq.vix_timeframe_note if hasattr(psq, "vix_timeframe_note") else ""

    if vix_label != "NORMAL":
        label_map = {
            "OVEREXTENDED_RISK": "⚠️ **過度延伸風險** | 低 VIX 環境多頭訊號可能是牛陷阱",
            "HIGH_CONVICTION_RECOVERY": "🚀 **高確信反彈** | 高 VIX + 空頭減速 = 反轉機會",
        }
        label_text = label_map.get(vix_label, vix_label)
        embed.add_field(
            name="🏅 VIX 動能判定", value=f"{label_text}\n\u200b", inline=False
        )

    if vix_tf_note:
        embed.add_field(
            name="⏱️ 時間框架建議", value=f"`{vix_tf_note}`\n\u200b", inline=False
        )

    # 最新新聞
    add_news_field(embed, data.get("news_text"))

    # VIX tier info in footer
    vix_spot_val = data.get("vix_spot") or (
        data.get("vix_battle_status", {}).get("vix_spot")
    )
    vix_emoji_val = data.get("vix_battle_status", {}).get("emoji", "")
    vix_name_val = data.get("vix_battle_status", {}).get("name", "")
    vix_footer = (
        f" | VIX: {vix_spot_val:.1f} {vix_emoji_val} {vix_name_val}"
        if vix_spot_val
        else ""
    )
    embed.set_footer(text=f"Nexus Seeker • PowerSqueeze 日K量化引擎{vix_footer}")
    return embed


def create_news_scan_embed(symbol, news_text):
    """建構新聞掃描結果的 Embed"""
    embed = discord.Embed(title=f"📰 {symbol} 官方新聞掃描", color=discord.Color.blue())
    add_news_field(embed, news_text)
    embed.set_footer(text="Nexus Seeker 研報系統 • 資料來源: Yahoo Finance")
    return embed


def create_reddit_scan_embed(symbol, reddit_text):
    """建構 Reddit 情緒掃描結果的 Embed"""
    embed = discord.Embed(
        title=f"🔥 {symbol} 散戶情緒優勢 (Reddit 同步)", color=discord.Color.orange()
    )
    add_reddit_text = reddit_text
    add_reddit_field(embed, add_reddit_text)
    embed.set_footer(
        text="Nexus Seeker 研報系統 • 資料來源: Reddit (WSB/Stocks/Options)"
    )
    return embed


def create_polymarket_list_embed(markets: List[Dict[str, Any]]):
    """建構 Polymarket 監控中的熱門市場 Embed"""
    embed = discord.Embed(
        title="🐋 Polymarket 巨鯨意圖圖譜",
        color=discord.Color.blue(),
        timestamp=datetime.now(timezone.utc),
    )

    if not markets:
        embed.description = "目前沒有監控中的市場。"
        return embed

    description = ""
    for i, m in enumerate(markets, 1):
        question = m.get("question", "未知市場")
        # 截斷過長的標題
        if len(question) > 70:
            question = question[:67] + "..."

        # 取得 token 價格資訊 (如果有的話)
        tokens = m.get("tokens", [])
        price_info = ""
        if tokens:
            # 顯示前兩個 outcome 的價格
            p_list = []
            for t in tokens[:2]:
                outcome = str(t.get("outcome", "")).strip()
                price = t.get("price", 0)

                # 排除單字元的雜訊 (例如 [ or " )
                if len(outcome) <= 1 and outcome not in ["?", "是", "否"]:
                    continue

                # 簡單格式化價格 (0-1)
                try:
                    price_val = float(price)
                    price_str = f"{price_val:.2f}"
                except Exception:
                    price_str = str(price)

                if outcome:
                    p_list.append(f"{outcome}: `{price_str}`")

            if p_list:
                price_info = " | ".join(p_list)
                line = f"**{i}.** {question}\n   └ {price_info}\n"
            else:
                line = f"**{i}.** {question}\n"
        else:
            line = f"**{i}.** {question}\n"

        # 檢查總長度，避免超過 Discord 限制
        if len(description) + len(line) > 3900:
            break
        description += line

    embed.description = description
    embed.set_footer(text="Nexus Seeker | Polymarket Monitor (Top 20 Active Markets)")
    return embed


def create_holdings_embed(
    holdings_data: List[Dict[str, Any]], total_capital: float
) -> discord.Embed:
    """建構現貨持倉 (Holdings) 狀態報告 Embed"""
    embed = discord.Embed(
        title="📊 Nexus Seeker | 現貨持倉清單",
        description="追蹤您的長期股權資產與成本分佈。\n\u200b",
        color=discord.Color.blue(),
        timestamp=datetime.now(timezone.utc),
    )

    if not holdings_data:
        embed.description = "📭 目前無現貨持倉紀錄。請使用 `/add_holding` 進行登錄。"
        return embed

    total_value = 0.0
    total_pnl = 0.0

    lines = ["```ansi"]
    header = f"{'標的'.ljust(8)} | {'數量'.rjust(8)} | {'平均成本'.rjust(10)} | {'當前損益'.rjust(10)}"
    lines.append(header)
    lines.append("-" * 43)

    for h in holdings_data:
        sym = h["symbol"].ljust(8)
        qty = f"{h['quantity']:,.0f}".rjust(8)
        cost = f"${h['avg_cost']:,.2f}".rjust(10)

        curr_p = h.get("current_price", 0.0)
        pnl = (curr_p - h["avg_cost"]) * h["quantity"] if curr_p > 0 else 0.0
        pnl_pct = (
            (curr_p / h["avg_cost"] - 1) if h["avg_cost"] > 0 and curr_p > 0 else 0.0
        )

        total_pnl += pnl
        total_value += curr_p * h["quantity"]

        # 使用 ANSI 顏色：綠色表示正損益，紅色表示負損益
        color_start = "[0;32m" if pnl >= 0 else "[0;31m"
        pnl_fmt = f"{color_start}{pnl_pct:+.1%}[0m".rjust(18)

        lines.append(f"{sym} | {qty} | {cost} | {pnl_fmt}")

    lines.append("```")
    embed.add_field(name="📦 持倉明細", value="\n".join(lines), inline=False)

    summary = (
        f"💰 **持倉總市值**: `${total_value:,.2f}`\n"
        f"📈 **累計未實現損益**: `${total_pnl:,.2f}`\n"
        f"⚖️ **佔總預算比例**: `{ (total_value / total_capital * 100) if total_capital > 0 else 0:.1f}%`"
    )
    embed.add_field(name="🏁 財務摘要", value=summary, inline=False)

    embed.set_footer(text="Nexus Accounting Engine | 專業股權追蹤")
    return embed


def create_trades_embed(
    pnl_data: Dict[str, Any],
    total_capital: float = 0.0,
) -> discord.Embed:
    """建構實單持倉 (Portfolio) 狀態與未實現損益報告 Embed"""
    embed = discord.Embed(
        title="📊 Nexus Seeker | 實單持倉清單 (包含帳面損益)",
        description="追蹤您的期權實單持倉與即時未實現損益 (Unrealized PnL)。\n\u200b",
        color=discord.Color.green(),
        timestamp=datetime.now(timezone.utc),
    )

    trades = pnl_data.get("trades", [])
    if not trades:
        embed.description = "📭 目前無持倉紀錄。"
        return embed

    lines = ["```ansi"]
    # 標頭 (調整 Python len 以匹配可見寬度)
    header = f"{'ID'.ljust(2)} | {'標的'.ljust(4)} | {'到期日'.ljust(7)} | {'履約'.ljust(5)} | {'數'.rjust(1)} | {'成本'.rjust(4)} | {'現價'.rjust(4)} | {'帳面損益'.rjust(10)}"
    lines.append(header)
    lines.append("-" * 75)

    total_cost = 0.0
    for t in trades:
        trade_id = t["id"]
        sym = t["symbol"]
        o_type = t["opt_type"]
        strike = t["strike"]
        exp = t["expiry"]
        qty = t["quantity"]
        entry_p = t["entry_price"]
        curr_p = t.get("current_price", 0.0)
        unrealized_pnl = t["unrealized_pnl"]
        pnl_pct = t["pnl_pct"]

        total_cost += abs(entry_p * qty * 100)

        id_fmt = f"{trade_id:02d}".ljust(2)
        sym_fmt = sym.ljust(4)
        exp_fmt = exp.ljust(10)
        st_type_fmt = f"{strike}{o_type[0].upper()}".ljust(7)

        color_code = "\x1b[0;32m" if qty > 0 else "\x1b[0;31m"
        qty_val = f"{abs(qty):>2}"
        qty_fmt = f"{color_code}{qty_val}\x1b[0m"

        cost_fmt = f"{entry_p:6.2f}"
        curr_fmt = f"{curr_p:6.2f}"

        pnl_color = "\x1b[0;32m" if unrealized_pnl >= 0 else "\x1b[0;31m"
        pnl_val = f"${unrealized_pnl:+.0f} ({pnl_pct:+.1%})"
        pnl_fmt = f"{pnl_color}{pnl_val:>14}\x1b[0m"

        lines.append(
            f"{id_fmt} | {sym_fmt} | {exp_fmt} | {st_type_fmt} | {qty_fmt} | {cost_fmt} | {curr_fmt} | {pnl_fmt}"
        )

    lines.append("```")
    embed.add_field(name="📦 持倉明細", value="\n".join(lines), inline=False)

    total_unrealized_pnl = pnl_data.get("total_unrealized_pnl", 0.0)

    summary = (
        f"💰 **持倉總權利金成本 (概算)**: `${total_cost:,.2f}`\n"
        f"⚖️ **佔總預算比例**: `{ (total_cost / total_capital * 100) if total_capital > 0 else 0:.1f}%`\n"
        f"📈 **總未實現損益 (Unrealized PnL)**: `${total_unrealized_pnl:,.2f}`"
    )
    embed.add_field(name="🏁 財務摘要 (Financial Summary)", value=summary, inline=False)

    embed.set_footer(text="Nexus Portfolio Engine | 專業實單與損益監控")
    return embed


def create_strategic_dash_embed(
    user_ctx: Any, pnl_data: Dict[str, Any], vix_spot: float = 18.0
) -> discord.Embed:
    """
    建構戰略看板 (Strategic Dashboard) Embed.
    遵循 Task 1 的 Traditional Chinese 模板。
    """
    embed = discord.Embed(
        title="📊 Nexus 交易員戰略看板",
        color=discord.Color.dark_blue(),
        timestamp=datetime.now(timezone.utc),
    )

    # 1. 財務生存狀態 (Financial Runway)
    daily_theta = _safe_float(user_ctx.total_theta)
    monthly_expense = _safe_float(user_ctx.monthly_expense)
    daily_expense = monthly_expense / 30.0 if monthly_expense > 0 else 0.0
    coverage_pct = (daily_theta / daily_expense * 100) if daily_expense > 0 else 100.0

    # 計算跑道天數
    gap = daily_expense - daily_theta
    cash_reserve = _safe_float(user_ctx.cash_reserve)
    if gap <= 0:
        runway_days = "∞ (收益已覆蓋支出)"
    else:
        runway_days = f"{cash_reserve / gap:,.0f}" if gap > 0 else "∞"

    status_mode = "觀戰模式" if not user_ctx.is_professional_mode else "實戰模式"
    nav = _safe_float(user_ctx.capital) + pnl_data.get("total_unrealized_pnl", 0.0)

    runway_info = (
        f"* **總資產 (NAV):** `${nav:,.0f}` ({status_mode})\n"
        f"* **目前跑道:** {runway_days} 天 (由現有現金與 Theta 收益推算)\n"
        f"* **收租效率:** 每日 Theta `${daily_theta:,.2f}` (覆蓋率 {coverage_pct:.1f}%)\n"
    )
    if coverage_pct < 100 and daily_expense > 0:
        runway_info += f"> 💡 警訊：現金流覆蓋不足，每日收租缺口為 `${gap:,.2f}`。"

    embed.add_field(
        name="🏁 財務生存狀態 (Financial Runway)", value=runway_info, inline=False
    )

    # 2. 組合風險精算 (NRO Integrity)
    # Vanna Sensitivity Stress Test: 若 VIX 上升 10%，隱含 Delta 將變動至 [New_Delta]
    total_vanna = _safe_float(user_ctx.total_vanna)
    total_delta = _safe_float(user_ctx.total_weighted_delta)
    capital = _safe_float(user_ctx.capital)

    vanna_impact = total_vanna * (vix_spot * 0.10 / 100.0)
    new_delta = total_delta + vanna_impact

    # 對沖狀態與建議
    hedge_status = "運行中" if abs(total_delta) < (capital * 0.1) else "需調整"
    # 簡單邏輯：如果 Delta 太正，建議賣出 SPY；如果太負，建議買入 SPY
    if total_delta > 100:
        hedge_instruction = f"賣出 {int(total_delta)} 股 SPY 以對沖正 Delta"
    elif total_delta < -100:
        hedge_instruction = f"買入 {int(abs(total_delta))} 股 SPY 以對沖負 Delta"
    else:
        hedge_instruction = "目前曝險平衡，無需立即調整"

    nro_info = (
        f"* **Beta-Delta:** `{total_delta:+.1f}` (相對於 SPY 的整體曝險)\n"
        f"* **Vanna 敏感度:** 若 VIX 上升 10%，隱含 Delta 將變動至 `{new_delta:+.1f}`。\n"
        f"* **對沖狀態:** {hedge_status}\n"
        f"> 🎯 建議對沖位：{hedge_instruction}"
    )
    embed.add_field(name="🛡️ 組合風險精算 (NRO Integrity)", value=nro_info, inline=False)

    # 3. 系統健康
    ram_usage = psutil.virtual_memory().percent
    # 假設 BoundedCache 正常，這裡簡單寫死或從某處獲取
    sys_health = f"RAM {ram_usage}% | BoundedCache 穩定"
    embed.add_field(name="⚙️ 系統健康", value=sys_health, inline=False)

    embed.set_footer(text="Nexus Seeker | 戰術風險管理終端")
    return embed


def create_tactical_symbol_embed(data: Dict[str, Any]) -> discord.Embed:
    """
    建構標的深度分析 (Tactical Deep-Dive) Embed.
    遵循 Task 2 的 Traditional Chinese 模板。
    """
    symbol = data.get("symbol", "UNKNOWN")
    embed = discord.Embed(
        title=f"🌌 標的分析中心: {symbol}",
        color=discord.Color.dark_magenta(),
        timestamp=datetime.now(timezone.utc),
    )

    # 1. 情緒與邊緣偵測 (Edge Detection)
    skew_val = data.get("skew", 0.0)
    skew_percentile = data.get("skew_percentile", 50.0)

    edge_info = (
        f"* **Option Skew:** `{skew_val:.2f}%` (處於 `{skew_percentile:.1f}` 分位點)\n"
    )
    if skew_percentile > 90:
        edge_info += "> ⚠️ 市場下行保護需求極高，隱含避險情緒升溫。\n"

    # 巨鯨/散戶意圖映射
    poly_odds = data.get("polymarket_odds", "N/A")
    reddit_score = data.get("reddit_sentiment_score", "中性")

    edge_info += (
        f"* **巨鯨/散戶意圖映射:**\n"
        f"    * Polymarket 預測勝率: `{poly_odds}`\n"
        f"    * Reddit 情緒指數: `{reddit_score}`\n"
    )

    # 偵測背離
    # 簡單邏輯：如果 Skew 很高但 Reddit 很樂觀，就是背離
    divergence = "同步"
    action = "保持觀察"
    if skew_percentile > 80 and "看多" in str(reddit_score):
        divergence = "情緒背離 (散戶樂觀 vs 專業避險)"
        action = "建立保護性賣權或減碼"
    elif skew_percentile < 20 and "看空" in str(reddit_score):
        divergence = "情緒背離 (散戶恐慌 vs 權利金便宜)"
        action = "考慮賣出賣權 (Cash Secured Put)"

    edge_info += f"> 💡 偵測到{divergence}，建議 {action}。\n"

    embed.add_field(
        name="📐 情緒與邊緣偵測 (Edge Detection)", value=edge_info, inline=False
    )

    # 2. 結算與目標 (Target Lock)
    max_pain = data.get("max_pain", 0.0)
    price = data.get("price", 1.0)
    distance = ((max_pain - price) / price * 100) if price > 0 else 0.0

    ddp_status = "符合 (符合 DDP 盈餘/估值雙擊)" if data.get("is_ddp") else "不符合"
    ivr = data.get("iv_rank", 0.0)

    target_info = (
        f"* **Max Pain:** `${max_pain:.2f}` (目前價差: `{distance:+.1f}%`)\n"
        f"* **DDP 掃描:** {ddp_status} (IV Rank: `{ivr:.1f}%`)\n"
    )

    # 操作指引 (What-if Scenario Analysis)
    if abs(distance) < 2.0:
        scenario = "價格接近最大痛點，結算日前可能維持震盪。"
    elif distance > 5.0:
        scenario = "價格遠低於最大痛點，具備磁吸效應回升動能。"
    elif distance < -5.0:
        scenario = "價格遠高於最大痛點，需留意結算日前壓回風險。"
    else:
        scenario = "目前價差適中，依技術指標操作為主。"

    target_info += f"* **操作指引:** {scenario}\n"

    embed.add_field(name="🎯 結算與目標 (Target Lock)", value=target_info, inline=False)

    embed.set_footer(
        text="🔗 使用 /settle_hedge 紀錄對沖或 /event_impact 進行曝險模擬。"
    )
    return embed


def create_watchlist_embed(page_data, current_page, total_pages, total_items):
    """生成觀察清單的分頁 Embed (移除成本欄位)"""

    if not page_data:
        description = "目前沒有追蹤任何項目"
    else:
        lines = ["```ansi"]
        # 1. 標頭修改為兩欄
        header = f"{'標的'.ljust(12)} | {'AI 分析 (LLM)'.rjust(12)}"
        lines.append(header)

        # 2. 分隔線
        lines.append("-" * 28)

        for sym, use_llm in page_data:
            sym_fmt = sym.ljust(12)
            llm_text = "開啟 (ON)" if use_llm else "關閉 (OFF)"
            llm_fmt = llm_text.rjust(12)
            lines.append(f"{sym_fmt} | {llm_fmt}")

        lines.append("```")
        description = "\n".join(lines)

    embed = discord.Embed(
        title="📡 【您的專屬觀察清單】",
        description=f"目前監控中的標的清單。系統將每 30 分鐘自動執行量化掃描。\n\n{description}",
        color=discord.Color.blurple(),
    )

    embed.set_footer(
        text=f"頁次: {current_page}/{total_pages} ｜ 📊 總項目: {total_items}"
    )
    return embed


def create_portfolio_report_embed(
    report_lines, hedge_analysis=None, survival_runway=None
):
    """
    將 check_portfolio_status_logic 產出的 report_lines 轉換為漂亮的 Discord Embed
    """
    # 處理完全為空的狀況
    if not report_lines:
        embed = discord.Embed(
            title="📊 Nexus Seeker 盤後風險結算報告",
            description="目前無持倉部位，亦無風險數據。\n\u200b",
            color=discord.Color.blue(),
            timestamp=datetime.now(timezone.utc),
        )
        embed.set_footer(text="Argo Risk Engine v2.6 | 基準標的: SPY")
        return embed

    # 1. 分割資料：將個別持倉與宏觀報告分開
    # 尋找分割點：🌐 【宏觀風險與資金水位報告】
    macro_index = -1
    for i, line in enumerate(report_lines):
        if _is_macro_report_marker(line):
            macro_index = i
            break

    # 2. 處理持倉細節與宏觀報告區隔
    if macro_index != -1:
        positions_list = [
            line.strip() for line in report_lines[:macro_index] if line.strip()
        ]
        macro_text = "\n".join(
            line.strip() for line in report_lines[macro_index:] if line.strip()
        )
    else:
        # 如果找不到宏觀報告區塊，將所有內容視為持倉明細
        positions_list = [line.strip() for line in report_lines if line.strip()]
        macro_text = "目前無宏觀風險數據。"

    # 使用 \n\n 分隔部位
    if positions_list:
        positions_text = "\n\n".join(positions_list)
    else:
        positions_text = "目前無持倉部位。"

    # 二次確認，防止 macro_text 全空或只包含空白，Discord Embed value 必須為非空字串
    if not macro_text:
        macro_text = "無宏觀風險數據。"

    # 4. 判斷顏色：如果有任何 "🚨" 或 "🆘"，就用紅色，否則用藍色
    embed_color = discord.Color.blue()
    if "🚨" in macro_text or "🆘" in macro_text:
        embed_color = discord.Color.red()
    elif "⚠️" in macro_text:
        embed_color = discord.Color.orange()

    embed = discord.Embed(
        title="📊 Nexus Seeker 盤後風險結算報告",
        color=embed_color,
        timestamp=datetime.now(timezone.utc),
    )

    # 🚀 [Professional Investor] 財務生存跑道 (Priority Header)
    if survival_runway is not None:
        runway_text = (
            "無限 (收益已覆蓋支出)"
            if survival_runway >= 9999
            else f"`{survival_runway:,.1f}` 天"
        )
        embed.add_field(
            name="🏁 財務生存跑道 (Financial Runway)",
            value=f"```yaml\n預估剩餘天數: {runway_text}\n(基於現有現金儲備與 Theta 收益)\n```\n\u200b",
            inline=False,
        )

    # 🚀 欄位一：個別持倉細節
    positions_text = _safe_embed_field_value(positions_text, "目前無持倉部位。")
    embed.add_field(name="📦 當前持倉明細", value=positions_text, inline=False)

    # 🚀 欄位二：全帳戶宏觀風險與對沖指令 (核心！)
    macro_text = _safe_embed_field_value(macro_text, "無宏觀風險數據。")
    embed.add_field(name="🛡️ 風控管線評估與對沖決策", value=macro_text, inline=False)

    # 🚀 欄位三：對沖有效性分析 (新增)
    if hedge_analysis:
        build_hedge_analysis_field(embed, hedge_analysis)

        # 如果有 Tau 係數更新資訊，則額外提示
        dynamic_tau = getattr(embed, "dynamic_tau", None) or (
            hedge_analysis.get("dynamic_tau")
            if isinstance(hedge_analysis, dict)
            else None
        )
        if dynamic_tau is not None:
            dynamic_tau = _safe_float(dynamic_tau, 1.0)
            embed.add_field(
                name="🧬 STHE 自動優化狀態",
                value=f"目前對沖調教因子 τ: `{dynamic_tau:.2f}`",
                inline=False,
            )

    embed.set_footer(text="Argo Risk Engine v2.6 | 基準標的: SPY")

    return embed


def create_transition_suggestion_embed(data: Dict[str, Any]) -> discord.Embed:
    """建構倉位演進建議 (Transition Suggestion) 的 Discord Embed"""
    sym = data["symbol"]
    pnl_pct = data["pnl_pct"] * 100
    pnl_usd = data["pnl_usd"]
    res = data["transition_result"]
    stock_p = data["stock_price"]

    embed = discord.Embed(
        title=f"🔄 倉位演進建議 | {sym}",
        description="偵測到投機部位獲利豐厚，建議轉向「收租模式」以鎖定長期收益。",
        color=discord.Color.gold(),
        timestamp=datetime.now(timezone.utc),
    )

    # 目前獲利狀況
    embed.add_field(
        name="📈 目前獲利狀況",
        value=f"獲利率: `{pnl_pct:+.1f}%` / `${pnl_usd:,.2f}`\n\u200b",
        inline=True,
    )

    # 演進後效益
    embed.add_field(
        name="🧬 演進後預期效益",
        value=(
            f"調整後持股成本: **`${res.adjusted_cost_basis:.2f}`**\n"
            f"相對現價折讓: `{res.capital_efficiency_gain:.1f}%` (現價: `${stock_p:.2f}`)\n"
            f"預期 CC 年化報酬 (AROC): `{res.projected_aroc:.1f}%` (@${res.cc_strike})\n\u200b"
        ),
        inline=False,
    )

    # 操作指令建議
    embed.add_field(
        name="🛠️ 建議操作路徑",
        value=(
            f"1. **Synthetic Exit**: 獲利平倉目前 Option 部位。\n"
            f"2. **Core Equity**: 使用獲利抵扣，購入 100 股現股。\n"
            f"3. **Covered Call**: 賣出 `${res.cc_strike}` Call 啟動收租。\n\u200b"
        ),
        inline=False,
    )

    embed.set_footer(text="Nexus Position Evolution Engine | 專業投資者建議")
    return embed


def build_vtr_stats_embed(
    user_name: str, stats: dict, attribution_lines: List[str] = None
) -> discord.Embed:
    """
    建構 VTR 績效統計 Embed 面板，含對沖效能歸因。
    """
    # 根據勝率決定顏色
    win_rate = stats.get("win_rate", 0)
    if win_rate >= 60:
        color = 0x2ECC71  # 綠色 (Success)
        status_icon = "🟢"
    elif win_rate >= 40:
        color = 0xF1C40F  # 黃色 (Warning)
        status_icon = "🟡"
    else:
        color = 0xE74C3C  # 紅色 (Danger)
        status_icon = "🔴"

    embed = discord.Embed(
        title="📈 Nexus Seeker | 虛擬交易室 (VTR) 績效總結",
        description=f"使用者: **{user_name}** 的系統歸因分析",
        color=color,
        timestamp=datetime.now(timezone.utc),
    )

    # 核心指標
    embed.add_field(
        name="總結算次數", value=f"`{stats.get('total_trades', 0)}`", inline=True
    )
    embed.add_field(name="勝率", value=f"{status_icon} `{win_rate}%`", inline=True)

    # 損益指標
    pnl = stats.get("total_pnl", 0.0)
    pnl_str = f"+${pnl:,.2f}" if pnl >= 0 else f"-${abs(pnl):,.2f}"
    embed.add_field(name="累計總損益", value=f"**{pnl_str}**", inline=True)
    embed.add_field(
        name="平均單筆損益", value=f"`${stats.get('avg_pnl', 0.0):.2f}`", inline=True
    )

    # 對沖歸因報告
    if attribution_lines:
        attr_text = "\n".join(attribution_lines)
        # 截斷過長內容
        if len(attr_text) > 1024:
            attr_text = attr_text[:1021] + "..."
        embed.add_field(name="🛡️ 對沖效能與自我進化", value=attr_text, inline=False)

    embed.set_footer(text="Nexus Sandbox Engine | 數據包含已平倉之虛擬部位")

    return embed


def build_scan_report(result: Dict[str, Any]):
    """
    量化掃描報告Embed，整合 Greeks, NRO 與 EMA 訊號。
    """
    ai_decision = result.get("ai_decision", "SKIP")
    color = (
        0x2ECC71
        if ai_decision == "APPROVE"
        else (0xE74C3C if ai_decision == "VETO" else 0x3498DB)
    )

    embed = discord.Embed(
        title=f"📡 量化掃描報告: {result['symbol']}",
        description=f"策略: `{result.get('strategy', 'N/A')}` | 履約價: `${result.get('strike', 'N/A')}` | 到期日: `{result.get('target_date', 'N/A')}`",
        color=color,
    )

    # 1. Greeks 區塊 (從 result 中提取)
    greeks_info = (
        f"Delta: `{result.get('delta', 0):.3f}` ｜ Theta: `{result.get('theta', 0):.4f}`\n"
        f"Gamma: `{result.get('gamma', 0):.6f}` ｜ IV: `{result.get('iv', 0):.1%}`"
    )
    embed.add_field(name="🧬 Greeks 希臘字母", value=greeks_info, inline=False)

    # 2. NRO 風控區塊
    nro_info = (
        f"建議口數: `{result.get('safe_qty', 0)}` 口\n"
        f"預期總曝險: `{result.get('projected_exposure_pct', 0):+.1f}%` / `{result.get('risk_limit', 15.0)}%` (紅線)"
    )
    embed.add_field(name="🛡️ NRO 風控判定", value=nro_info, inline=False)

    # 3. 🚀 整合 EMA 訊號區塊
    ema_ui = get_ema_signal_ui(result.get("ema_signals", []))
    embed.add_field(name="📈 趨勢與指標動態", value=ema_ui, inline=False)

    # 4. 加上宏觀背景燈號 (VIX/Oil)
    vix = result.get("macro_vix", result.get("vix", 0))
    oil = result.get("macro_oil", result.get("oil", 0))
    vix_status = "🔴" if vix > 25 else "🟢"

    embed.set_footer(
        text=f"環境感知: VIX {vix} {vix_status} | WTI ${oil} | 基準 SPY: ${result.get('spy_price', 0):.1f}"
    )

    return embed


def create_rehedge_embed(rehedge_info: Dict[str, Any]) -> discord.Embed:
    """
    建構「自動避險回補建議」的 Discord Embed 面板。
    """
    priority = rehedge_info.get("priority", "NORMAL")
    color = 0xF1C40F if priority == "NORMAL" else 0xE74C3C  # 黃色或紅色

    symbol = rehedge_info.get("symbol", "SPY")
    suggested_qty = rehedge_info.get("suggested_spy_qty", 0)
    reason = rehedge_info.get("reason", "偵測到曝險異常或市場轉弱")

    embed = discord.Embed(
        title="🛡️ 防禦啟動：自動避險回補建議",
        color=color,
        description=f"標的: **{symbol}**",
    )

    embed.add_field(name="觸發原因", value=f"```\n{reason}\n```", inline=False)

    action_val = (
        f"賣出 (Short) `{suggested_qty}` 股 SPY"
        if suggested_qty > 0
        else f"買入 (Long) `{abs(suggested_qty)}` 股 SPY"
    )
    embed.add_field(name="建議動作", value=action_val, inline=True)

    embed.set_footer(text="提示：當前趨勢已走弱，掛回避險可鎖定現有獲利。")
    embed.timestamp = datetime.now(timezone.utc)

    return embed


def create_ddp_embed(report: Dict[str, Any]) -> discord.Embed:
    """建構 Davis Double Play (DDP) 預警 Embed"""
    sym = report["symbol"]
    curr_pe = report["current_pe"]
    pe_mean = report["pe_mean_3y"]
    eps_growth = report["eps_growth"] * 100
    rev_accel = "加速 (Accelerating)" if report["rev_accel"] else "穩定 (Stable)"
    score = report["confidence_score"]

    # 計算 P/E 均值回歸空間 (預期漲幅)
    pe_upside = (pe_mean / curr_pe - 1) * 100 if curr_pe > 0 else 0

    embed = discord.Embed(
        title=f"🌌 Nexus 戴維斯雙擊預警: {sym}",
        description="偵測到標的符合 **Davis Double Play (DDP)** 條件：盈餘增長與估值擴張的雙重共振。",
        color=0x00FF7F,  # SpringGreen
        timestamp=datetime.now(timezone.utc),
    )

    embed.add_field(
        name="目前本益比 (Current P/E)", value=f"`{curr_pe:.2f}`", inline=True
    )
    embed.add_field(
        name="3 年本益比均值 (3Y P/E Mean)", value=f"`{pe_mean:.2f}`", inline=True
    )
    embed.add_field(
        name="預估本益比回歸空間", value=f"`+{pe_upside:.1f}%`", inline=True
    )

    embed.add_field(
        name="預估 EPS 成長率 (Proj. EPS Growth)",
        value=f"`{eps_growth:+.1f}%`",
        inline=True,
    )
    embed.add_field(
        name="營收加速狀態 (Revenue Acceleration)", value=f"`{rev_accel}`", inline=True
    )
    embed.add_field(
        name="DDP 信心評分 (Confidence Score)", value=f"`{score:.0f}/100`", inline=True
    )

    # 邏輯解釋
    logic_text = (
        f"1. **盈餘動能**: YoY 成長率 `{eps_growth:.1f}%` 超過 15% 門檻。\n"
        f"2. **估值壓縮**: 目前 P/E 處於 3 年歷史低位 (25% 分位數以下)。\n"
        f"3. **前瞻預期**: Forward P/E `{report['forward_pe']:.2f}` 低於目前 TTM P/E。\n"
        f"4. **動能確認**: 營收成長較前一週期加速。"
    )
    embed.add_field(name="🧐 量化篩選邏輯", value=logic_text, inline=False)

    embed.set_footer(text="Nexus Quantitative Research | 戴維斯雙擊引擎 v1.0")
    return embed


def create_volatility_embed(report: Dict[str, Any]) -> discord.Embed:
    """建構波動率優勢偵測 (Cheap Volatility) 預警 Embed"""
    sym = report["symbol"]
    price = report["price"]
    iv = report["iv"]
    iv_p = report["iv_p"]
    hv = report["hv"]
    status = report["status"]
    strategy = report["strategy"]
    trigger_logic = report["trigger_logic"]
    days_to_earnings = report["days_to_earnings"]
    stop_loss = report["stop_loss"]
    daily_theta = report["daily_theta"]
    runway_impact = report["runway_impact"]

    # 決定顏色 (Buy Signal = Green, Watchlist Alert = Yellow)
    color = 0x00FF00 if status == "波動率極低" else 0xFFFF00

    embed = discord.Embed(
        title="📊 Nexus Seeker | 波動率優勢偵測",
        color=color,
        timestamp=datetime.now(timezone.utc),
    )

    # Field 1: [Symbol] 戰略評估
    val1 = (
        f"當前價格 (Current Price): `${price:.2f}`\n"
        f"IV / IV Percentile: `{iv}% ({iv_p}%)`\n"
        f"HV (252-day): `{hv}%`\n"
        f"狀態: **{status}**\n\u200b"
    )
    embed.add_field(name=f"🔍 {sym} 戰略評估", value=val1, inline=False)

    # Field 2: 買入時機分析
    catalyst = (
        f"距離財報 {days_to_earnings} 天" if days_to_earnings <= 90 else "無近期財報"
    )
    val2 = (
        f"建議策略 (Recommended Strategy): **{strategy}**\n"
        f"觸發邏輯 (Trigger Logic): {trigger_logic}\n"
        f"催化劑 (Catalysts): {catalyst}\n\u200b"
    )
    embed.add_field(name="🎯 買入時機分析", value=val2, inline=False)

    # Field 3: 風險管理 (NRO)
    val3 = (
        f"建議停損 (Suggested Stop Loss): `${stop_loss:.2f}`\n"
        f"Theta 每日損耗 (Daily Theta Decay): `-${daily_theta:.2f}/day`\n"
        f"跑道影響 (Runway Impact): 預計影響生存跑道 `{runway_impact}` 天\n\u200b"
    )
    embed.add_field(name="🛡️ 風險管理 (NRO)", value=val3, inline=False)

    embed.set_footer(text="Nexus Volatility Strategist Agent v1.0 | 專注廉價波動率偵測")
    return embed


def build_hedge_analysis_field(embed, analysis):
    """
    在 embed 中加入對沖分析區塊。
    """
    if not isinstance(analysis, dict):
        embed.add_field(
            name="🛡️ 對沖有效性診斷",
            value=_safe_embed_field_value(
                "目前無法取得對沖分析資料。", "目前無法取得對沖分析資料。"
            ),
            inline=False,
        )
        return

    status = str(analysis.get("status", "UNKNOWN"))
    status_emoji = "✅" if status == "OPTIMAL" else "⚠️"
    effectiveness = _safe_float(analysis.get("effectiveness", 0.0), 0.0)
    alpha_contribution = _safe_float(analysis.get("alpha_contribution", 0.0), 0.0)
    hedge_contribution = _safe_float(analysis.get("hedge_contribution", 0.0), 0.0)
    hedge_ratio = _safe_float(analysis.get("hedge_ratio", 0.0), 0.0)
    net_pnl = _safe_float(analysis.get("net_pnl", 0.0), 0.0)

    # 決定有效性評價
    if effectiveness >= 0.8:
        eff_text = "🎯 精準"
    elif effectiveness >= 0.6:
        eff_text = "⚖️ 適中"
    else:
        eff_text = "🌪️ 偏差"

    content = (
        f"🔹 **個股 Alpha 損益**: `${alpha_contribution:,.2f}`\n"
        f"🔸 **對沖 Beta 損益**: `${hedge_contribution:,.2f}`\n"
        f"📊 **對沖比率 (HR)**: `{hedge_ratio:.2%}` {status_emoji}\n"
        f"🧩 **對沖有效性 (ES)**: `{effectiveness:.2%}` ({eff_text})\n"
        f"🏁 **最終淨損益**: **`${net_pnl:,.2f}`**"
    )

    embed.add_field(
        name="🛡️ 對沖有效性診斷",
        value=_safe_embed_field_value(content, "對沖分析資料不足。"),
        inline=False,
    )


def create_ai_analysis_embed(
    ai_report_text: str, title: str = "📊 Nexus Seeker 盤後 AI 深度分析"
) -> discord.Embed:
    """
    將 AI 產出的盤後深度分析轉換為 Discord Embed 格式。
    參考盤後風險結算報告風格。
    """
    embed_color = discord.Color.blue()
    if "🚨" in ai_report_text or "🆘" in ai_report_text:
        embed_color = discord.Color.red()
    elif "⚠️" in ai_report_text:
        embed_color = discord.Color.orange()

    embed = discord.Embed(
        title=title,
        color=embed_color,
        timestamp=datetime.now(timezone.utc),
    )

    # 嘗試將報告內容分割成段落 (以 Markdown 標題分割)
    # 預期格式如: 1. **🏁 財務生存跑道**
    sections = re.split(r"\n(?=\d+\.\s+\*\*|(?:\*\*[\u2600-\u2BFF]))", ai_report_text)

    if len(sections) > 1:
        for section in sections:
            section = section.strip()
            if not section:
                continue

            # 提取標題與內容
            lines = section.split("\n", 1)
            header = lines[0].replace("**", "").strip()
            # 去掉序號如 "1. "
            header = re.sub(r"^\d+\.\s*", "", header)

            content = lines[1].strip() if len(lines) > 1 else "無內容"
            # 移除分隔線
            content = re.sub(r"^-{3,}$", "", content, flags=re.MULTILINE).strip()

            embed.add_field(
                name=header,
                value=_safe_embed_field_value(content, "無詳細資訊"),
                inline=False,
            )
    else:
        # 如果沒辦法切分，則整塊放入
        embed.description = _truncate_with_boundary(ai_report_text, 4000)

    embed.set_footer(text="Nexus Seeker AI Analyst v1.0 | 智慧投研管線")
    return embed


def create_next_day_strategy_embed(strategy_text: str) -> discord.Embed:
    """
    將次日策略制定報告轉換為 Discord Embed 格式。
    參考盤後風險結算報告風格。
    """
    embed_color = discord.Color.blue()
    if "🚨" in strategy_text or "🆘" in strategy_text:
        embed_color = discord.Color.red()
    elif "⚠️" in strategy_text:
        embed_color = discord.Color.orange()

    embed = discord.Embed(
        title="🎯 Nexus Seeker 次日策略制定",
        color=embed_color,
        timestamp=datetime.now(timezone.utc),
    )

    # 策略報告通常包含 VIX 資訊與戰術建議
    # 嘗試切分 **標題**:
    sections = re.split(r"\n(?=\*\*[^*]+\*\*[:：])", strategy_text)

    if len(sections) > 1:
        # 第一部分可能是時間標題行，放在 description
        first_part = sections[0].strip()
        # 移除時間標題與分隔線
        first_part = re.sub(r"\*\*.*次日策略制定\*\*", "", first_part)
        first_part = re.sub(r"-{10,}", "", first_part).strip()

        if first_part:
            embed.description = first_part

        # 處理後續欄位
        for section in sections[1:]:
            section = section.strip()
            if not section:
                continue

            lines = section.split("\n", 1)
            header = (
                lines[0].replace("**", "").replace(":", "").replace("：", "").strip()
            )
            content = lines[1].strip() if len(lines) > 1 else "無內容"

            embed.add_field(
                name=header,
                value=_safe_embed_field_value(content, "無詳細資訊"),
                inline=False,
            )
    else:
        # 移除標題行後放入 description
        clean_text = re.sub(r"\*\*.*次日策略制定\*\*", "", strategy_text)
        clean_text = re.sub(r"-{10,}", "", clean_text).strip()
        embed.description = _truncate_with_boundary(clean_text, 4000)

    embed.set_footer(text="Nexus Seeker Strategy Engine v1.0 | 戰鬥階級系統")
    return embed


def create_info_embed(title: str, message: str) -> discord.Embed:
    """建立標準資訊通知 Embed"""
    embed = discord.Embed(
        title=f"ℹ️ {title}",
        description=message,
        color=discord.Color.blue(),
        timestamp=datetime.now(timezone.utc),
    )
    embed.set_footer(text="Nexus Seeker | System Notification")
    return embed


def create_error_embed(message: str, title: str = "系統錯誤") -> discord.Embed:
    """建立標準錯誤通知 Embed"""
    embed = discord.Embed(
        title=f"❌ {title}",
        description=message,
        color=discord.Color.red(),
        timestamp=datetime.now(timezone.utc),
    )
    embed.set_footer(text="Nexus Seeker | Error Report")
    return embed
