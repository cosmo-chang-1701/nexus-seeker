import discord
import logging

from datetime import datetime, timezone
from market_analysis.portfolio import calculate_beta
from typing import List, Dict, Any, Optional

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

    boundary_candidates = [candidate.rfind("\n\n"), candidate.rfind("\n"), candidate.rfind("。")]
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
    colors = {"STO_PUT": discord.Color.green(), "STO_CALL": discord.Color.red(), "BTO_CALL": discord.Color.blue(), "BTO_PUT": discord.Color.orange()}
    titles = {"STO_PUT": "🟢 Sell To Open Put", "STO_CALL": "🔴 Sell To Open Call", "BTO_CALL": "🚀 Buy To Open Call", "BTO_PUT": "⚠️ Buy To Open Put"}

    is_covered = (strategy == "STO_CALL" and stock_cost > 0.0)
    if is_covered:
        titles["STO_CALL"] = "🛡️ Covered Call (掩護性買權)"
        colors["STO_CALL"] = discord.Color.teal()

    embed = discord.Embed(
        title=f"{titles.get(strategy, strategy)} | {data.get('symbol', 'UNKNOWN')}",
        description=f"📅 **到期日:** `{data.get('target_date', 'UNKNOWN')}` ｜ 🎯 **履約價:** `${data.get('strike', 'UNKNOWN')}`\n\u200b",
        color=colors.get(strategy, discord.Color.default())
    )
    return embed, is_covered

def _add_market_overview_fields(embed, data):
    beta = data.get('beta', 1.0)
    beta_status = "🚀" if beta > 1.3 else ("⚖️" if beta >= 0.8 else "🧊")
    embed.add_field(name="🏷️ 標價 / Beta\u2800\u2800", value=f"${data['price']:.2f} / `{beta:.2f}` {beta_status}\n\u200b", inline=True)
    embed.add_field(name="📈 RSI / 20MA\u2800\u2800\u2800", value=f"{data['rsi']:.2f} / ${data['sma20']:.2f}\n\u200b", inline=True)
    hvr_status = "🔥 高" if data['hv_rank'] >= 50 else ("⚡ 中" if data['hv_rank'] >= 30 else "🧊 低")
    embed.add_field(name="🔥 HV Rank\u2800\u2800\u2800\u2800", value=f"`{data['hv_rank']:.1f}%` {hvr_status}\n\u200b", inline=True)

def _add_volatility_fields(embed, data, strategy):
    vrp_pct = data.get('vrp', 0.0) * 100
    vrp_icon = "✅" if (("STO" in strategy and vrp_pct > 0) or ("BTO" in strategy and vrp_pct < 0)) else "⚠️"
    embed.add_field(name="⚖️ VRP 溢酬\u2800\u2800\u2800\u2800", value=f"`{vrp_pct:+.2f}%` {vrp_icon}\n\u200b", inline=True)
    ts_ratio_str = f"`{data['ts_ratio']:.2f}` {data['ts_state']}"
    embed.add_field(name="⏳ 個股 IV 期限結構\u2800\u2800", value=f"{ts_ratio_str}\n\u200b", inline=True)
    v_skew_str = f"`{data['v_skew']:.2f}` {data.get('v_skew_state', '')}"
    embed.add_field(name="📉 垂直偏態\u2800\u2800\u2800\u2800", value=f"{v_skew_str}\n\u200b", inline=True)

    # ---------------- VIX 306 Volatility Metrics ----------------
    vts_str = f"`{data.get('vix_vts_ratio', 1.0):.2f}` {data.get('vix_regime', '')}"
    embed.add_field(name="🌐 大盤 VIX 期限結構\u2800", value=f"{vts_str}\n\u200b", inline=True)
    
    z30 = data.get('vix_z30', 0.0)
    z60 = data.get('vix_z60', 0.0)
    z_icon = "📈 擴張" if z30 > 0.5 and z60 > 0 else "📉 收斂"
    embed.add_field(name="🔥 VIX 30/60 Z-Score", value=f"Z30:`{z30:.1f}` Z60:`{z60:.1f}` {z_icon}\n\u200b", inline=True)

    tail_risk = "🚨 觸發降規 (1/4 Kelly)" if data.get('is_high_tail_risk', False) else "✅ 正常 (1/2 Kelly)"
    embed.add_field(name="🛡️ 尾部風險管理\u2800\u2800\u2800", value=f"{tail_risk}\n\u200b", inline=True)

def _add_performance_and_kelly_fields(embed, data, user_capital):
    """添加績效與風控（含凱利倉位計算）欄位，並校正部位方向"""
    strategy = data.get('strategy', '')
    raw_delta = data.get('delta', 0.0)
    weighted_delta = data.get('weighted_delta', 0.0)

    # 🚀 方向校正邏輯：
    # 若是賣方 (STO)，部位方向 = 合約方向 * -1 (賣出負 Delta 是看多，賣出正 Delta 是看空)
    # 若是買方 (BTO)，部位方向 = 合約方向 (買入什麼就是什麼)
    pos_multiplier = -1 if "STO" in strategy else 1
    pos_weighted_shares = weighted_delta * pos_multiplier

    # 1. 希臘字母與部位方向
    embed.add_field(
        name="🧩 Delta (部位加權)\u2800\u2800", 
        value=f"{raw_delta:.3f} (`{pos_weighted_shares:+.1f}`股)\n\u200b", 
        inline=True
    )
    
    # 2. 獲利效率與隱含波動率
    embed.add_field(
        name="💰 AROC / IV\u2800\u2800\u2800\u2800", 
        value=f"`{data['aroc']:.1f}%` / {data['iv']:.1%}\n\u200b", 
        inline=True
    )

    # 3. 凱利建議邏輯
    alloc_pct = data.get('alloc_pct', 0.0)
    suggested = data.get('suggested_contracts', 0)
    
    if alloc_pct <= 0:
        kelly_value = "`不建議建倉`"
    elif not user_capital or user_capital <= 0:
        kelly_value = f"`未設資金` ({alloc_pct*100:.1f}%)"
    else:
        # 使用與主邏輯同步的 25% Kelly 上限顯示
        kelly_value = f"`{suggested} 口` ({min(alloc_pct, 0.25)*100:.1f}%)" if suggested > 0 else "`本金不足`"
    
    embed.add_field(name="🧮 凱利原始建議\u2800\u2800", value=f"{kelly_value}\n\u200b", inline=True)

def _add_earnings_fields(embed, data, strategy):
    """添加財報預期波動欄位"""
    if 0 <= data.get('earnings_days', -1) <= 14:
        mmm_str = f"±{data['mmm_pct']:.1f}% (倒數 {data['earnings_days']} 天)"
        bounds_str = f"🛡️ 安全區間: **`${data['safe_lower']:.2f}`** ~ **`${data['safe_upper']:.2f}`**"
        strike = data['strike']
        
        if "STO" in strategy:
            is_safe = (strategy == "STO_PUT" and strike <= data['safe_lower']) or \
                      (strategy == "STO_CALL" and strike >= data['safe_upper'])
            safety_icon = "✅ 避開雷區 (適宜收租)" if is_safe else "💣 位於雷區 (極高風險)"
        else:
            safety_icon = "🎲 財報盲盒 (注意 IV Crush 波動率壓縮風險)"
            
        embed.add_field(name="📊 財報預期波動 (MMM)", value=f"`{mmm_str}`\n{bounds_str}\n{safety_icon}\n\u200b", inline=False)

def _add_covered_call_fields(embed, data, stock_cost):
    """添加 Covered Call 專屬防護欄位"""
    bid = data.get('bid', 0)
    true_breakeven = stock_cost - bid
    yoc = (bid / stock_cost) * 100 if stock_cost > 0 else 0
    
    cc_info = (f"📦 **真實現股成本:** `${stock_cost:.2f}`\n"
               f"🛡️ **真實下檔防線:** `${true_breakeven:.2f}`\n"
               f"💸 **單次收租殖利率 (Yield on Cost):** `{yoc:.2f}%`\n"
               f"👉 *您的持倉成本已透過收租進一步降低！*\n\u200b")
    embed.add_field(name="🛡️ Covered Call 專屬防護", value=cc_info, inline=False)

def _add_expected_move_fields(embed, data, strategy, is_covered):
    """添加預期波動區間與損益兩平防線欄位"""
    em = data.get('expected_move', 0.0)
    em_lower = data.get('em_lower', 0.0)
    em_upper = data.get('em_upper', 0.0)
    
    if "STO_PUT" in strategy:
        breakeven = data['strike'] - data.get('bid', 0)
        safe = breakeven < em_lower
        safety_text = "✅ 防線已建構於預期暴跌區間外" if safe else "⚠️ 損益兩平點位於預期波動區間內，風險較高"
        em_info = f"1σ 預期下緣: `${em_lower:.2f}` (預期最大跌幅 -${em:.2f})\n🛡️ 損益兩平點: **`${breakeven:.2f}`**\n{safety_text}\n\u200b"
        embed.add_field(name="🎯 機率圓錐 (1σ 預期波動)", value=em_info, inline=False)
        
    elif "STO_CALL" in strategy:
        breakeven = data['strike'] + data.get('bid', 0)
        safe = breakeven > em_upper
        if is_covered:
            safety_text = "✅ 若漲破此價位，將以最高獲利出場 (股票被 Call 走)"
        else:
            safety_text = "✅ 防線已建構於預期暴漲區聯外" if safe else "⚠️ 損益兩平點位於預期波動區間內，風險較高"
            
        em_info = f"1σ 預期上緣: `${em_upper:.2f}` (預期最大漲幅 +${em:.2f})\n🛡️ 合約兩平點: **`${breakeven:.2f}`**\n{safety_text}\n\u200b"
        embed.add_field(name="🎯 機率圓錐 (1σ 預期波動)", value=em_info, inline=False)

    elif "BTO_PUT" in strategy:
        breakeven = data['strike'] - data.get('ask', 0)
        em_info = f"1σ 預期下緣: `${em_lower:.2f}` (預期最大跌幅 -${em:.2f})\n🛡️ 損益兩平點: **`${breakeven:.2f}`**\n✅ 目標跌破此防線即開始獲利\n\u200b"
        embed.add_field(name="🎯 機率圓錐 (1σ 預期波動)", value=em_info, inline=False)

    elif "BTO_CALL" in strategy:
        breakeven = data['strike'] + data.get('ask', 0)
        em_info = f"1σ 預期上緣: `${em_upper:.2f}` (預期最大漲幅 +${em:.2f})\n🛡️ 損益兩平點: **`${breakeven:.2f}`**\n✅ 目標突破此防線即開始獲利\n\u200b"
        embed.add_field(name="🎯 機率圓錐 (1σ 預期波動)", value=em_info, inline=False)

def _add_liquidity_fields(embed, data):
    """添加報價與流動性分析欄位"""
    mid_price = data.get('mid_price', (data.get('bid', 0) + data.get('ask', 0)) / 2)
    liq_status = data.get('liq_status', 'N/A')
    liq_msg = data.get('liq_msg', '')

    spread_info = (f"**Bid:** `{data.get('bid', 0):.2f}` ｜ **Ask:** `{data.get('ask', 0):.2f}` (價差 `{data.get('spread_ratio', 0):.1f}%`)\n"
                   f"**狀態:** {liq_status} {liq_msg}\n"
                   f"🎯 **Limit (中價掛單建議):** `{mid_price:.2f}`\n\u200b")
    embed.add_field(name="💱 報價與流動性分析", value=spread_info, inline=False)

def _add_strategy_upgrade_fields(embed, data, strategy):
    """添加策略升級提示欄位"""
    if strategy in ["BTO_CALL", "BTO_PUT"]:
        hedge_strike = data.get('suggested_hedge_strike')
        if hedge_strike:
            spread_type = "多頭價差 (Bull Call Spread)" if strategy == "BTO_CALL" else "空頭價差 (Bear Put Spread)"
            hedge_type = "Call" if strategy == "BTO_CALL" else "Put"
            
            upgrade_text = (f"為抵銷 Theta (時間價值) 衰減並降低建倉成本，\n"
                            f"建議在買入本合約的同時，賣出更價外的 **${hedge_strike:.0f} {hedge_type}**\n"
                            f"👉 組合為: **{spread_type}**\n\u200b")
            embed.add_field(name="💡 經理人策略升級建議", value=upgrade_text, inline=False)

def _add_risk_optimization_fields(embed, data, user_capital=None):
    """
    添加事前曝險模擬與自動風控優化建議
    🚀 強化版：增加閾值動態化與基準價校驗
    """
    projected_pct = data.get('projected_exposure_pct')
    # 若無數據則不顯示 (注意：不要用 if projected_pct == 0)
    if projected_pct is None:
        return

    safe_qty = data.get('safe_qty', 0)
    hedge_spy = data.get('hedge_spy', 0.0)
    suggested = data.get('suggested_contracts', 0)
    
    # 🚀 修正點 1：風險閾值應從數據中取得，或設為全局變數
    # 避免後台改了 10% 這裡還在顯示 15%
    RISK_THRESHOLD = data.get('risk_limit_pct', 15.0) 
    
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
    
    embed.add_field(name=f"🛡️ What-if 曝險模擬 | {sim_status}", value=f"{sim_block}\n\u200b", inline=False)

    # 2. Nexus Risk Optimizer 自動優化建議
    if suggested > safe_qty:
        opt_title = "⚖️ Nexus Risk Optimizer (自動優化建議)"
        
        # 🚀 修正點 2：加入基準 SPY 價格的動態提示 (讓對沖建議更可信)
        spy_p = data.get('spy_price', 690.0)
        
        actions = [f"--- 偵測到風險超標，執行自動降規 ---"]
        actions.append(f"❌ 原始建議: {suggested} 口")
        actions.append(f"✅ 安全成交: {safe_qty} 口 (符合風控)")
        
        if safe_qty == 0 and hedge_spy != 0:
            actions.append(f"\n⚠️ 警告: 即使下 1 口也過載")
            direction = "賣出" if hedge_spy > 0 else "買入"
            # 格式化對沖股數，避免出現 22.2222222
            actions.append(f"🛡️ 建議對沖: {direction} {abs(hedge_spy):.1f} 股 SPY (@${spy_p:.1f})")
        
        opt_block = "```diff\n" + "\n".join(actions) + "\n```"
        embed.add_field(name=opt_title, value=f"{opt_block}\n\u200b", inline=False)

def _add_hedge_unlock_fields(embed, data):
    """添加對沖解除建議欄位 (Hedge Unlocking)"""
    unlock = data.get('hedge_unlock')
    if not unlock:
        return

    symbol = data.get('symbol', 'N/A')
    suggested_qty = unlock.get('reduce_spy_qty', 0)
    new_delta = unlock.get('new_delta', 0.0)
    reason = unlock.get('reason', '')
    risk_note = unlock.get('risk_note', '')

    # 依照使用者要求的文案格式
    unlock_text = (
        f"偵測到 **{symbol}** 強勢突破。目前您的 SPY 對沖正在產生 Hedge Drag。\n\n"
        f"✅ **建議動作：** 買回/平倉 `{suggested_qty}` 股 SPY。\n"
        f"🚀 **預計效應：** 釋放 Beta 動能，預計提升總組合 Delta 至 `{new_delta:+.1f}`。\n"
        f"🛡️ **防禦補償：** {risk_note}\n\u200b"
    )
    
    embed.add_field(
        name=f"🔓 對沖優化建議 ({reason})",
        value=unlock_text,
        inline=False
    )

def _add_ai_verification_fields(embed, data):
    """添加 AI 驗證決策欄位"""
    ai_decision = data.get('ai_decision')
    ai_reasoning = data.get('ai_reasoning')
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
        window = sig['window']
        sig_type = sig['type']
        direction = sig['direction']
        dist = sig['distance_pct']
        
        # 1. 決定圖示與標題
        if sig_type == "CROSSOVER":
            icon = "🚀" if direction == "BULLISH" else "💀"
            action = "強勢突破" if direction == "BULLISH" else "失守跌破"
        else: # TEST
            icon = "🛡️" if direction == "SUPPORT" else "🛑"
            action = "回測支撐" if direction == "SUPPORT" else "觸碰壓力"

        # 2. 格式化輸出
        line = f"{icon} **EMA {window} {action}** (偏離: `{dist}%`)"
        ui_lines.append(line)

    return "\n".join(ui_lines)

def _add_trend_and_support_fields(embed, data):
    """添加 EMA 狀態圖形化燈號欄位"""
    trend = data.get('trend', 'UNKNOWN')
    ema21 = data.get('ema_21', 0.0)
    distance = data.get('distance_from_21', 0.0)

    if trend == "BULLISH_STRONG":
        trend_str = "📈 Strong Bullish (Price > 8 > 21)"
    elif trend == "BULLISH_CORRECTION":
        trend_str = "📉 Bullish Correction (EMA 8 > 21 ≥ Price)"
    elif trend == "BEARISH_STRONG":
        trend_str = "🐻 Strong Bearish (Price < 8 < 21)"
    else:
        trend_str = "⚖️ Neutral Trend"

    # Risk 判定
    if distance > 10.0:
        risk_str = "⚠️ Overextended (Gap > 10%)"
    elif distance < -10.0:
        risk_str = "⚠️ Oversold (Gap < -10%)"
    else:
        risk_str = "✅ Stable Zone"

    support_str = f"EMA 21 at ${ema21:.2f} (Gap: {distance:+.1f}%)"

    trend_info = f"**Trend:** {trend_str}\n**Support:** {support_str}\n**Risk:** {risk_str}\n"
    trend_info += get_ema_signal_ui(data.get('ema_signals', []))

    embed.add_field(name="🧭 趨勢與支撐 (EMA 8/21)", value=trend_info + "\n\u200b", inline=False)

def create_scan_embed(data, user_capital=100000.0):
    strategy = data.get('strategy', 'UNKNOWN')
    stock_cost = data.get('stock_cost', 0.0)
    
    embed, is_covered = _build_embed_base(data, strategy, stock_cost)
    
    # 依序渲染 UI
    _add_market_overview_fields(embed, data)
    _add_volatility_fields(embed, data, strategy)
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
    
    add_news_field(embed, data.get('news_text'))
    add_reddit_field(embed, data.get('reddit_text'))
    _add_ai_verification_fields(embed, data)

    # 🚀 AlertFilter 推播理由 (僅在條件式過濾觸發時顯示)
    alert_reason = data.get('alert_reason')
    if alert_reason:
        embed.add_field(
            name="📢 推播觸發條件",
            value=f"```\n{alert_reason}\n```",
            inline=False,
        )

    embed.set_footer(text=f"Nexus Seeker 風控引擎 • 基準 SPY: ${data.get('spy_price', 500):.1f}")
    return embed

def create_news_scan_embed(symbol, news_text):
    """建構新聞掃描結果的 Embed"""
    embed = discord.Embed(
        title=f"📰 {symbol} 官方新聞掃描", 
        color=discord.Color.blue()
    )
    add_news_field(embed, news_text)
    embed.set_footer(text="Nexus Seeker 研報系統 • 資料來源: Yahoo Finance")
    return embed

def create_reddit_scan_embed(symbol, reddit_text):
    """建構 Reddit 情緒掃描結果的 Embed"""
    embed = discord.Embed(
        title=f"🔥 {symbol} 散戶情緒掃描", 
        color=discord.Color.orange()
    )
    add_reddit_text = reddit_text
    add_reddit_field(embed, add_reddit_text)
    embed.set_footer(text="Nexus Seeker 研報系統 • 資料來源: Reddit (WSB/Stocks/Options)")
    return embed


def create_watchlist_embed(page_data, current_page, total_pages, total_items):
    """生成觀察清單的分頁 Embed (使用等寬區塊排版)"""
    
    if not page_data:
        description = "目前沒有追蹤任何項目"
    else:
        lines = ["```ansi"] # 使用 ansi 可支援文字變色，或純用 ``` 即可
        
        # 1. 標頭修改為四欄
        header = f"{'標的'.ljust(8)} | {'狀態'.ljust(7)} | {'成本'.rjust(8)} | {'LLM'.rjust(3)}"
        lines.append(header)
        
        # 2. 分隔線配合四欄總長度加長
        lines.append("-" * 37) 
        
        for sym, cost, use_llm in page_data:
            sym_fmt = sym.ljust(8)
            
            # 3. 將狀態與成本拆分為獨立變數
            if cost > 0:
                status_text = "📦 持倉"
                cost_text = f"${cost:.2f}"
            else:
                status_text = "🔍 觀察"
                cost_text = "-"
                
            status_fmt = status_text.ljust(7)
            cost_fmt = cost_text.rjust(8) 
            
            llm_icon = "🟢" if use_llm else "🔴"
            llm_fmt = llm_icon.rjust(3)
            
            # 4. 組合四欄輸出
            lines.append(f"{sym_fmt} | {status_fmt} | {cost_fmt} | {llm_fmt}")
            
        lines.append("```")
        description = "\n".join(lines)
        
    embed = discord.Embed(
        title=f"📡 【您的專屬觀察清單】",
        description=description,
        color=discord.Color.blurple()
    )
    
    embed.set_footer(text=f"頁次: {current_page}/{total_pages} ｜ 📊 總項目: {total_items}")
    return embed

def create_portfolio_report_embed(report_lines, hedge_analysis=None):
    """
    將 check_portfolio_status_logic 產出的 report_lines 轉換為漂亮的 Discord Embed
    """
    # 處理完全為空的狀況
    if not report_lines:
        embed = discord.Embed(
            title="📊 Nexus Seeker 盤後風險結算報告",
            description="目前無持倉部位，亦無風險數據。\n\u200b",
            color=discord.Color.blue(),
            timestamp=datetime.now(timezone.utc)
        )
        embed.set_footer(text="Argo Risk Engine v2.5 | 基準標的: SPY")
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
        positions_list = [line.strip() for line in report_lines[:macro_index] if line.strip()]
        macro_text = "\n".join(line.strip() for line in report_lines[macro_index:] if line.strip())
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
        timestamp=datetime.now(timezone.utc)
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
        dynamic_tau = getattr(embed, 'dynamic_tau', None) or (hedge_analysis.get('dynamic_tau') if isinstance(hedge_analysis, dict) else None)
        if dynamic_tau is not None:
            dynamic_tau = _safe_float(dynamic_tau, 1.0)
            embed.add_field(name="🧬 STHE 自動優化狀態", value=f"目前對沖調教因子 τ: `{dynamic_tau:.2f}`", inline=False)

    embed.set_footer(text="Argo Risk Engine v2.5 | 基準標的: SPY")
    
    return embed

def build_vtr_stats_embed(user_name: str, stats: dict) -> discord.Embed:
    """
    建構 VTR 績效統計 Embed 面板
    """
    # 根據勝率決定顏色
    win_rate = stats['win_rate']
    if win_rate >= 60:
        color = 0x2ecc71  # 綠色 (Success)
        status_icon = "🟢"
    elif win_rate >= 40:
        color = 0xf1c40f  # 黃色 (Warning)
        status_icon = "🟡"
    else:
        color = 0xe74c3c  # 紅色 (Danger)
        status_icon = "🔴"

    embed = discord.Embed(
        title=f"📈 Nexus Seeker | 虛擬交易室 (VTR) 績效週報",
        description=f"使用者: **{user_name}** 的自動化回測數據",
        color=color,
        timestamp=datetime.now()
    )

    # 核心指標
    embed.add_field(name="總結算次數", value=f"`{stats['total_trades']}`", inline=True)
    embed.add_field(name="勝率", value=f"{status_icon} `{win_rate}%`", inline=True)
    
    # 損益指標 (使用 LaTeX 格式強調數值)
    pnl = stats['total_pnl']
    pnl_str = f"+${pnl:,}" if pnl >= 0 else f"-${abs(pnl):,}"
    embed.add_field(name="累計總損益", value=f"**{pnl_str}**", inline=True)
    embed.add_field(name="平均單筆損益", value=f"`${stats['avg_pnl']}`", inline=True)

    # 腳註與提示
    embed.set_footer(text="數據包含已平倉 (CLOSED) 與 已轉倉 (ROLLED) 之合約")
    
    return embed

def build_scan_report(result: Dict[str, Any]):
    """
    量化掃描報告Embed，整合 Greeks, NRO 與 EMA 訊號。
    """
    ai_decision = result.get('ai_decision', 'SKIP')
    color = 0x2ecc71 if ai_decision == 'APPROVE' else (0xe74c3c if ai_decision == 'VETO' else 0x3498db)
    
    embed = discord.Embed(
        title=f"📡 量化掃描報告: {result['symbol']}",
        description=f"策略: `{result.get('strategy', 'N/A')}` | 履約價: `${result.get('strike', 'N/A')}` | 到期日: `{result.get('target_date', 'N/A')}`",
        color=color
    )

    # 1. Greeks 區塊 (從 result 中提取)
    greeks_info = (f"Delta: `{result.get('delta', 0):.3f}` ｜ Theta: `{result.get('theta', 0):.4f}`\n"
                  f"Gamma: `{result.get('gamma', 0):.6f}` ｜ IV: `{result.get('iv', 0):.1%}`")
    embed.add_field(name="🧬 Greeks 希臘字母", value=greeks_info, inline=False)

    # 2. NRO 風控區塊
    nro_info = (f"建議口數: `{result.get('safe_qty', 0)}` 口\n"
               f"預期總曝險: `{result.get('projected_exposure_pct', 0):+.1f}%` / `{result.get('risk_limit_pct', 15.0)}%` (紅線)")
    embed.add_field(name="🛡️ NRO 風控判定", value=nro_info, inline=False)

    # 3. 🚀 整合 EMA 訊號區塊
    ema_ui = get_ema_signal_ui(result.get('ema_signals', []))
    embed.add_field(
        name="📈 趨勢與指標動態",
        value=ema_ui,
        inline=False
    )

    # 4. 加上宏觀背景燈號 (VIX/Oil)
    vix = result.get('macro_vix', result.get('vix', 0))
    oil = result.get('macro_oil', result.get('oil', 0))
    vix_status = "🔴" if vix > 25 else "🟢"
    
    embed.set_footer(text=f"環境感知: VIX {vix} {vix_status} | WTI ${oil} | 基準 SPY: ${result.get('spy_price', 0):.1f}")
    
    return embed

def create_rehedge_embed(rehedge_info: Dict[str, Any]) -> discord.Embed:
    """
    建構「自動避險回補建議」的 Discord Embed 面板。
    """
    priority = rehedge_info.get('priority', 'NORMAL')
    color = 0xf1c40f if priority == "NORMAL" else 0xe74c3c # 黃色或紅色
    
    symbol = rehedge_info.get('symbol', 'SPY')
    suggested_qty = rehedge_info.get('suggested_spy_qty', 0)
    reason = rehedge_info.get('reason', '偵測到曝險異常或市場轉弱')

    embed = discord.Embed(
        title="🛡️ 防禦啟動：自動避險回補建議", 
        color=color,
        description=f"標的: **{symbol}**"
    )
    
    embed.add_field(name="觸發原因", value=f"```\n{reason}\n```", inline=False)
    
    action_val = f"賣出 (Short) `{suggested_qty}` 股 SPY" if suggested_qty > 0 else f"買入 (Long) `{abs(suggested_qty)}` 股 SPY"
    embed.add_field(name="建議動作", value=action_val, inline=True)
    
    embed.set_footer(text="提示：當前趨勢已走弱，掛回避險可鎖定現有獲利。")
    embed.timestamp = datetime.now(timezone.utc)
    
    return embed

def build_hedge_analysis_field(embed, analysis):
    """
    在 embed 中加入對沖分析區塊。
    """
    if not isinstance(analysis, dict):
        embed.add_field(
            name="🛡️ 對沖有效性診斷",
            value=_safe_embed_field_value("目前無法取得對沖分析資料。", "目前無法取得對沖分析資料。"),
            inline=False,
        )
        return

    status = str(analysis.get('status', 'UNKNOWN'))
    status_emoji = "✅" if status == "OPTIMAL" else "⚠️"
    effectiveness = _safe_float(analysis.get('effectiveness', 0.0), 0.0)
    alpha_contribution = _safe_float(analysis.get('alpha_contribution', 0.0), 0.0)
    hedge_contribution = _safe_float(analysis.get('hedge_contribution', 0.0), 0.0)
    hedge_ratio = _safe_float(analysis.get('hedge_ratio', 0.0), 0.0)
    net_pnl = _safe_float(analysis.get('net_pnl', 0.0), 0.0)
    
    # 決定有效性評價
    if effectiveness >= 0.8: eff_text = "🎯 精準"
    elif effectiveness >= 0.6: eff_text = "⚖️ 適中"
    else: eff_text = "🌪️ 偏差"

    content = (
        f"🔹 **個股 Alpha 損益**: `${alpha_contribution:,.2f}`\n"
        f"🔸 **對沖 Beta 損益**: `${hedge_contribution:,.2f}`\n"
        f"📊 **對沖比率 (HR)**: `{hedge_ratio:.2%}` {status_emoji}\n"
        f"🧩 **對沖有效性 (ES)**: `{effectiveness:.2%}` ({eff_text})\n"
        f"🏁 **最終淨損益**: **`${net_pnl:,.2f}`**"
    )
    
    embed.add_field(name="🛡️ 對沖有效性診斷", value=_safe_embed_field_value(content, "對沖分析資料不足。"), inline=False)