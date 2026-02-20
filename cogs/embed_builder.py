import discord
import logging

logger = logging.getLogger(__name__)


def create_scan_embed(data, user_capital=100000.0):
    """根據掃描結果資料建構 Discord Embed 訊息。

    這是一個純格式化函式，不依賴任何外部狀態或資料庫。

    Args:
        data: 來自 market_math.analyze_symbol() 的結果字典。
        user_capital: 使用者的總作戰資金。

    Returns:
        discord.Embed 物件。
    """
    colors = {"STO_PUT": discord.Color.green(), "STO_CALL": discord.Color.red(), "BTO_CALL": discord.Color.blue(), "BTO_PUT": discord.Color.orange()}
    titles = {"STO_PUT": "🟢 Sell To Open Put", "STO_CALL": "🔴 Sell To Open Call", "BTO_CALL": "🚀 Buy To Open Call", "BTO_PUT": "⚠️ Buy To Open Put"}
    embed = discord.Embed(title=f"{titles[data['strategy']]} - {data['symbol']}", color=colors.get(data['strategy'], discord.Color.default()))
    
    # 展示標的現價
    embed.add_field(name="標的現價", value=f"${data['price']:.2f}")
    
    # 展示 RSI/20MA
    embed.add_field(name="RSI/20MA", value=f"{data['rsi']:.2f} / ${data['sma20']:.2f}")
    
    # 展示 HVR (波動率位階)
    hvr_status = "🔥 高" if data['hv_rank'] >= 50 else ("⚡ 中" if data['hv_rank'] >= 30 else "🧊 低")
    embed.add_field(name="HV Rank (波動率位階)", value=f"`{data['hv_rank']:.1f}%` {hvr_status}")

    # 展示 VRP (波動率風險溢酬)
    vrp_pct = data.get('vrp', 0.0) * 100
    # 賣方需要正溢酬，買方反而偏好負溢酬(買入便宜的波動率)
    if "STO" in data['strategy']:
        vrp_icon = "✅ 溢價 (具備數學優勢)" if vrp_pct > 0 else "⚠️ 折價 (期望值為負)"
    else:
        vrp_icon = "✅ 折價 (買方成本低估)" if vrp_pct < 0 else "⚠️ 溢價 (買方成本過高)"
    embed.add_field(name="VRP (波動率風險溢酬)", value=f"`{vrp_pct:+.2f}%` {vrp_icon}")

    # 展示 IV 期限結構 (Term Structure)
    ts_ratio_str = f"`{data['ts_ratio']:.2f}`"
    # 若發生倒掛，給予強烈視覺提示
    if data['ts_ratio'] >= 1.05:
        ts_ratio_str = f"**{ts_ratio_str}** {data['ts_state']} 🎯"
    else:
        ts_ratio_str = f"{ts_ratio_str} {data['ts_state']}"
    embed.add_field(name="IV 期限結構 (30D/60D)", value=ts_ratio_str)

    # 展示垂直波動率偏態 (Vertical Skew)
    v_skew_str = f"`{data['v_skew']:.2f}` {data.get('v_skew_state', '')}"
    if data.get('v_skew') >= 1.30:
        v_skew_str = f"**{data['v_skew']:.2f}** {data.get('v_skew_state', '')}"
    embed.add_field(name="垂直偏態 (Put/Call IV Ratio)", value=v_skew_str)
    
    # 展示 AROC (年化報酬率)
    embed.add_field(name="AROC (年化報酬率)", value=f"`{data['aroc']:.1f}%` 💰")

    # 凱利準則部位建議
    alloc_pct = data.get('alloc_pct', 0.0)
    margin_per_contract = data.get('margin_per_contract', 0.0)
    MAX_KELLY_ALLOC = 0.25  # 硬性上限：最多 25% 資金，避免過度集中

    if alloc_pct <= 0:
        # 凱利比例為負或零，代表數學期望值不足，不應建倉
        kelly_value = "`不建議建倉` (凱利比例為負，數學期望值不足)"
    elif not user_capital or user_capital <= 0:
        # 使用者尚未設定資金
        kelly_value = f"`尚未設定資金` (請使用 /set_capital 設定，建議佔比 {alloc_pct*100:.1f}%)"
    elif margin_per_contract <= 0:
        # 保證金資料異常
        kelly_value = "`保證金資料異常` (無法計算建議口數)"
    else:
        # 套用 Half-Kelly 上限，避免凱利公式在高勝率時建議過度集中
        capped_alloc_pct = min(alloc_pct, MAX_KELLY_ALLOC)
        allocated_capital = user_capital * capped_alloc_pct
        suggested_contracts = int(allocated_capital // margin_per_contract)

        if suggested_contracts > 0:
            total_margin = suggested_contracts * margin_per_contract
            cap_note = f" ⚠️ 已套用上限 {MAX_KELLY_ALLOC*100:.0f}%" if alloc_pct > MAX_KELLY_ALLOC else ""
            kelly_value = f"`{suggested_contracts} 口` (佔總資金 {capped_alloc_pct*100:.1f}%, 約 ${total_margin:,.0f}){cap_note}"
        else:
            kelly_value = f"`本金門檻不足` (建議佔比 {alloc_pct*100:.1f}%, 每口保證金 ${margin_per_contract:,.0f})"

    embed.add_field(name="⚖️ 凱利準則建議倉位", value=kelly_value)

    # 財報預期波動與雷區判定
    if 0 <= data.get('earnings_days', -1) <= 14:
        mmm_str = f"±{data['mmm_pct']:.1f}% (倒數 {data['earnings_days']} 天)"
        bounds_str = f"下緣 ${data['safe_lower']:.2f} / 上緣 ${data['safe_upper']:.2f}"
        
        strike = data['strike']
        strategy = data['strategy']
        
        if "STO" in strategy:
            is_safe = (strategy == "STO_PUT" and strike <= data['safe_lower']) or \
                      (strategy == "STO_CALL" and strike >= data['safe_upper'])
            safety_icon = "✅ 避開雷區 (適宜收租)" if is_safe else "💣 位於雷區 (極高風險)"
        else:
            # 買方 (BTO) 其實期待突破 MMM 區間
            safety_icon = "🎲 財報盲盒 (注意 IV Crush 波動率壓縮風險)"
            
        embed.add_field(name="📊 財報預期波動 (MMM)", value=f"`{mmm_str}`\n{bounds_str}\n{safety_icon}", inline=False)
        
    embed.add_field(name="精算合約", value=f"{data['target_date']} (${data['strike']})", inline=False)

    # 預期波動區間 (Expected Move) 與 損益兩平防線
    em = data.get('expected_move', 0.0)
    em_lower = data.get('em_lower', 0.0)
    em_upper = data.get('em_upper', 0.0)
    
    if "STO_PUT" in data['strategy']:
        breakeven = data['strike'] - data['bid']
        safe = breakeven < em_lower
        safety_text = "✅ 防線已建構於預期暴跌區間外" if safe else "⚠️ 損益兩平點位於預期波動區間內，風險較高"
        em_info = f"1σ 預期下緣: `${em_lower:.2f}` (預期最大跌幅 -${em:.2f})\n" \
                f"🛡️ 損益兩平點: **`${breakeven:.2f}`**\n" \
                f"{safety_text}"
        embed.add_field(name="🎯 機率圓錐 (1σ 預期波動)", value=em_info, inline=False)
        
    elif "STO_CALL" in data['strategy']:
        breakeven = data['strike'] + data['bid']
        safe = breakeven > em_upper
        safety_text = "✅ 防線已建構於預期暴漲區間外" if safe else "⚠️ 損益兩平點位於預期波動區間內，風險較高"
        em_info = f"1σ 預期上緣: `${em_upper:.2f}` (預期最大漲幅 +${em:.2f})\n" \
                f"🛡️ 損益兩平點: **`${breakeven:.2f}`**\n" \
                f"{safety_text}"
        embed.add_field(name="🎯 機率圓錐 (1σ 預期波動)", value=em_info, inline=False)

    elif "BTO_PUT" in data['strategy']:
        breakeven = data['strike'] - data['ask']
        em_info = f"1σ 預期下緣: `${em_lower:.2f}` (預期最大跌幅 -${em:.2f})\n🛡️ 損益兩平點: **`${breakeven:.2f}`**\n✅ 目標跌破此防線即開始獲利"
        embed.add_field(name="🎯 機率圓錐 (1σ 預期波動)", value=em_info, inline=False)

    elif "BTO_CALL" in data['strategy']:
        breakeven = data['strike'] + data['ask']
        em_info = f"1σ 預期上緣: `${em_upper:.2f}` (預期最大漲幅 +${em:.2f})\n🛡️ 損益兩平點: **`${breakeven:.2f}`**\n✅ 目標突破此防線即開始獲利"
        embed.add_field(name="🎯 機率圓錐 (1σ 預期波動)", value=em_info, inline=False)

    # 報價與流動性分析 (Bid/Ask & Spread)
    mid_price = data.get('mid_price', (data['bid'] + data['ask']) / 2)
    liq_status = data.get('liq_status', 'N/A')
    liq_msg = data.get('liq_msg', '')

    spread_info = (f"`Bid ${data['bid']:.2f}` / `Ask ${data['ask']:.2f}`\n"
                   f"└ 價差: `${data['spread']:.2f}` ({data['spread_ratio']:.1f}%)\n"
                   f"└ 狀態: {liq_status}\n"
                   f"└ 📝 {liq_msg}\n"
                   f"🎯 **建議掛單價 (Limit): `${mid_price:.2f}`**")
    embed.add_field(name="報價與流動性分析", value=spread_info, inline=False)

    # 策略升級提示
    if data['strategy'] in ["BTO_CALL", "BTO_PUT"]:
        hedge_strike = data.get('suggested_hedge_strike')
        if hedge_strike:
            # 判斷是牛市價差還是熊市價差
            spread_type = "多頭價差 (Bull Call Spread)" if data['strategy'] == "BTO_CALL" else "空頭價差 (Bear Put Spread)"
            hedge_type = "Call" if data['strategy'] == "BTO_CALL" else "Put"
            
            upgrade_text = (f"為抵銷 Theta (時間價值) 衰減並降低建倉成本，\n"
                            f"建議在買入本合約的同時，賣出更價外的 **${hedge_strike:.0f} {hedge_type}**\n"
                            f"👉 組合為: **{spread_type}**")
            
            embed.add_field(name="💡 經理人策略升級建議", value=upgrade_text, inline=False)

    embed.add_field(name="Delta / 當前合約 IV", value=f"{data['delta']:.3f} / {data['iv']:.1%}")
    
    return embed
