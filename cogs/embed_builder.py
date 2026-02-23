import discord
import logging

logger = logging.getLogger(__name__)

def create_scan_embed(data, user_capital=100000.0):
    """根據掃描結果資料建構 Discord Embed 訊息。"""
    colors = {"STO_PUT": discord.Color.green(), "STO_CALL": discord.Color.red(), "BTO_CALL": discord.Color.blue(), "BTO_PUT": discord.Color.orange()}
    titles = {"STO_PUT": "🟢 Sell To Open Put", "STO_CALL": "🔴 Sell To Open Call", "BTO_CALL": "🚀 Buy To Open Call", "BTO_PUT": "⚠️ Buy To Open Put"}
    
    strategy = data.get('strategy', 'UNKNOWN')
    stock_cost = data.get('stock_cost', 0.0)
    
    # 🛡️ 如果是 Covered Call，覆寫標題與顏色
    is_covered = (strategy == "STO_CALL" and stock_cost > 0.0)
    if is_covered:
        titles["STO_CALL"] = "🛡️ Covered Call (掩護性買權)"
        colors["STO_CALL"] = discord.Color.teal() # 使用特殊的藍綠色代表安全防護

    # === 標題與描述 ===
    embed = discord.Embed(
        title=f"{titles.get(strategy, strategy)} | {data.get('symbol', 'UNKNOWN')}",
        description=f"📅 **到期日:** `{data.get('target_date', 'UNKNOWN')}` ｜ 🎯 **履約價:** `${data.get('strike', 'UNKNOWN')}`\n\u200b",
        color=colors.get(strategy, discord.Color.default())
    )
    
    # --- 第一排（當前概況） ---
    embed.add_field(name="🏷️ 標的現價\u2800\u2800\u2800\u2800", value=f"${data['price']:.2f}\n\u200b", inline=True)
    embed.add_field(name="📈 RSI / 20MA\u2800\u2800\u2800", value=f"{data['rsi']:.2f} / ${data['sma20']:.2f}\n\u200b", inline=True)
    
    hvr_status = "🔥 高" if data['hv_rank'] >= 50 else ("⚡ 中" if data['hv_rank'] >= 30 else "🧊 低")
    embed.add_field(name="🔥 HV Rank\u2800\u2800\u2800\u2800", value=f"`{data['hv_rank']:.1f}%` {hvr_status}\n\u200b", inline=True)

    # --- 第二排（進階波動率） ---
    vrp_pct = data.get('vrp', 0.0) * 100
    if "STO" in data['strategy']:
        vrp_icon = "✅ 溢價" if vrp_pct > 0 else "⚠️ 折價"
    else:
        vrp_icon = "✅ 折價" if vrp_pct < 0 else "⚠️ 溢價"
    embed.add_field(name="⚖️ VRP 溢酬\u2800\u2800\u2800\u2800", value=f"`{vrp_pct:+.2f}%` {vrp_icon}\n\u200b", inline=True)

    ts_ratio_str = f"`{data['ts_ratio']:.2f}`"
    if data['ts_ratio'] >= 1.05:
        ts_ratio_str = f"**{ts_ratio_str}** {data['ts_state']} 🎯"
    else:
        ts_ratio_str = f"{ts_ratio_str} {data['ts_state']}"
    embed.add_field(name="⏳ IV 期限結構\u2800\u2800\u2800", value=f"{ts_ratio_str}\n\u200b", inline=True)

    v_skew_str = f"`{data['v_skew']:.2f}` {data.get('v_skew_state', '')}"
    if data.get('v_skew') >= 1.30:
        v_skew_str = f"**{data['v_skew']:.2f}** {data.get('v_skew_state', '')}"
    embed.add_field(name="📉 垂直偏態\u2800\u2800\u2800\u2800", value=f"{v_skew_str}\n\u200b", inline=True)
    
    # --- 第三排（績效與風控） ---
    embed.add_field(name="🧩 Delta / IV\u2800\u2800\u2800", value=f"{data['delta']:.3f} / {data['iv']:.1%}\n\u200b", inline=True)
    embed.add_field(name="💰 AROC\u2800\u2800\u2800\u2800\u2800", value=f"`{data['aroc']:.1f}%` 💰\n\u200b", inline=True)

    alloc_pct = data.get('alloc_pct', 0.0)
    margin_per_contract = data.get('margin_per_contract', 0.0)
    MAX_KELLY_ALLOC = 0.25

    if alloc_pct <= 0:
        kelly_value = "`不建議建倉`"
    elif not user_capital or user_capital <= 0:
        kelly_value = f"`尚未設定資金` ({alloc_pct*100:.1f}%)"
    elif margin_per_contract <= 0:
        kelly_value = "`資料異常`"
    else:
        capped_alloc_pct = min(alloc_pct, MAX_KELLY_ALLOC)
        allocated_capital = user_capital * capped_alloc_pct
        suggested_contracts = int(allocated_capital // margin_per_contract)

        if suggested_contracts > 0:
            kelly_value = f"`{suggested_contracts} 口` (佔總資金 {capped_alloc_pct*100:.1f}%)"
        else:
            kelly_value = f"`本金門檻不足` ({alloc_pct*100:.1f}%)"

    embed.add_field(name="🧮 凱利建議倉位\u2800\u2800", value=f"{kelly_value}\n\u200b", inline=True)

    # --- 單行特別資訊 ---
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

    # === 🛡️ 新增：Covered Call 專屬真實防線 ===
    if is_covered:
        bid = data.get('bid', 0)
        true_breakeven = stock_cost - bid
        yoc = (bid / stock_cost) * 100 if stock_cost > 0 else 0
        
        cc_info = (f"📦 **真實現股成本:** `${stock_cost:.2f}`\n"
                   f"🛡️ **真實下檔防線:** `${true_breakeven:.2f}`\n"
                   f"💸 **單次收租殖利率 (Yield on Cost):** `{yoc:.2f}%`\n"
                   f"👉 *您的持倉成本已透過收租進一步降低！*\n\u200b")
        embed.add_field(name="🛡️ Covered Call 專屬防護", value=cc_info, inline=False)

    # === 預期波動區間 (Expected Move) 與 損益兩平防線 ===
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
        # 🔥 如果是 CC，突破上方不是風險，而是獲利出場
        if is_covered:
            safety_text = "✅ 若漲破此價位，將以最高獲利出場 (股票被 Call 走)"
        else:
            safety_text = "✅ 防線已建構於預期暴漲區間外" if safe else "⚠️ 損益兩平點位於預期波動區間內，風險較高"
            
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

    # === 報價與流動性分析 ===
    mid_price = data.get('mid_price', (data.get('bid', 0) + data.get('ask', 0)) / 2)
    liq_status = data.get('liq_status', 'N/A')
    liq_msg = data.get('liq_msg', '')

    spread_info = (f"**Bid:** `{data.get('bid', 0):.2f}` ｜ **Ask:** `{data.get('ask', 0):.2f}` (價差 `{data.get('spread_ratio', 0):.1f}%`)\n"
                   f"**狀態:** {liq_status} {liq_msg}\n"
                   f"🎯 **Limit (中價掛單建議):** `{mid_price:.2f}`")
    embed.add_field(name="💱 報價與流動性分析", value=spread_info, inline=False)

    # === 策略升級提示 ===
    if strategy in ["BTO_CALL", "BTO_PUT"]:
        hedge_strike = data.get('suggested_hedge_strike')
        if hedge_strike:
            spread_type = "多頭價差 (Bull Call Spread)" if strategy == "BTO_CALL" else "空頭價差 (Bear Put Spread)"
            hedge_type = "Call" if strategy == "BTO_CALL" else "Put"
            
            upgrade_text = (f"為抵銷 Theta (時間價值) 衰減並降低建倉成本，\n"
                            f"建議在買入本合約的同時，賣出更價外的 **${hedge_strike:.0f} {hedge_type}**\n"
                            f"👉 組合為: **{spread_type}**")
            embed.add_field(name="💡 經理人策略升級建議", value=upgrade_text, inline=False)

    ai_decision = data.get('ai_decision')
    ai_reasoning = data.get('ai_reasoning')

    if ai_decision:
        if ai_decision == "APPROVE":
            ai_title = "🤖 Argo Cortex: ✅ 交易批准 (APPROVE)"
            # 正常放行，使用一般灰底程式碼區塊
            ai_value = f"```\n{ai_reasoning}\n```"
        elif ai_decision == "VETO":
            ai_title = "🤖 Argo Cortex: ⛔ 否決交易 (VETO 黑天鵝警告)"
            # 觸發黑天鵝警報，使用 diff 語法呈現紅字，並強制覆寫左側飾條顏色為深紅色
            ai_value = f"```diff\n- 警告: {ai_reasoning}\n```"
            embed.color = discord.Color.dark_red()
        elif ai_decision == "SKIP":
            ai_title = "🤖 Argo Cortex: ⚠️ 未啟用 (SKIP)"
            # 未啟用，使用一般灰底程式碼區塊
            ai_value = f"```\n{ai_reasoning}\n```"
            embed.color = discord.Color.blue()
            
        embed.add_field(name=ai_title, value=ai_value, inline=False)

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
            
            llm_icon = "🤖" if use_llm else "⚪"
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