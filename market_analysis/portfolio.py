import yfinance as yf
import pandas as pd
from datetime import datetime
from py_vollib.black_scholes_merton.greeks.analytical import delta, theta, gamma
from config import RISK_FREE_RATE

def _evaluate_defense_status(quantity, opt_type, pnl_pct, current_delta, dte):
    """
    動態防禦決策樹 (獨立負責判斷單一部位的生命週期與風險)
    """
    if quantity < 0: 
        # 賣方防禦邏輯 (Short Premium)
        if pnl_pct >= 0.50:
            return "✅ **建議停利** ｜ 獲利達 50% (Buy to Close)"
        if pnl_pct <= -1.50:
            return "☠️ **強制停損** ｜ 虧損達 150% (黑天鵝警戒)"
        if opt_type == 'put' and current_delta <= -0.40:
            return "🚨 **動態轉倉** ｜ Put Delta 擴張 (Roll Down & Out)"
        if opt_type == 'call' and current_delta >= 0.40:
            return "🚨 **動態轉倉** ｜ Call Delta 擴張 (Roll Up & Out)"
        # 🔥 新增：21 DTE Gamma 陷阱防禦
        if dte <= 21:
            return "⚠️ **Gamma 陷阱** ｜ DTE ≤ 21 (建議平倉或轉倉)"
    else:
        # 買方防禦邏輯 (Long Premium)
        if pnl_pct >= 1.0:
            return "✅ **建議停利** ｜ 獲利達 100% (Sell to Close)"
        if pnl_pct <= -0.50:
            return "⚠️ **停損警戒** ｜ 本金回撤達 50%"
        if dte <= 21:
            return "🚨 **動能衰竭** ｜ DTE ≤ 21 (建議平倉保留殘值)"
            
    return "⏳ **繼續持有** ｜ 未達防禦觸發條件"

def _calculate_macro_risk(total_beta_delta, total_theta, total_margin_used, total_gamma, user_capital):
    """
    計算投資組合的宏觀系統性風險、Theta 收益率、資金熱度極限 與 淨 Gamma 脆性
    """
    lines = ["", "🌐 **【宏觀系統性風險與資金水位評估】**", ""]
    
    # 1. 系統性方向風險 (Delta)
    lines.append(f"🔹 **投資組合淨 Delta:** `{total_beta_delta:+.2f}` (等同持有 SPY 股數)")
    if total_beta_delta > 50:
        lines.append("   🚨 **多頭曝險過高:** 建議建立 SPY 避險空單中和。")
    elif total_beta_delta < -50:
        lines.append("   🚨 **空頭曝險過高:** 建議建立大盤避險多單。")
    else:
        lines.append("   ✅ **風險中性 (Delta Neutral):** 受系統性崩盤影響較小。")
    lines.append("")

    # 🔥 2. 新增：非線性加速度與脆性評估 (Gamma)
    # 這裡的 Gamma 代表當 SPY 變動 $1 時，您的 Delta 會變動多少
    lines.append(f"🔹 **投資組合淨 Gamma:** `{total_gamma:+.2f}` (Delta 加速度 / 脆性指標)")
    if total_gamma < -20.0:
        lines.append("   🚨 **脆性警告 (High Fragility):** 淨 Gamma 極度偏負！")
        lines.append("      黑天鵝發生時 Delta 將瞬間失控。建議買入遠期 OTM 選擇權注入正 Gamma 緩衝。")
    elif total_gamma > 20.0:
        lines.append("   🛡️ **反脆弱 (Antifragile):** 淨 Gamma 偏正。大盤波動越劇烈，Delta 變化越有利。")
    else:
        lines.append("   ✅ **Gamma 中性:** 非線性加速度受控，帳戶淨值曲線平滑。")
    lines.append("")

    # 3. Theta 收益率精算
    theta_yield = (total_theta / user_capital) * 100 if user_capital > 0 else 0
    lines.append(f"🔹 **預估每日 Theta 現金流:** `${total_theta:+.2f}` (佔總資金 `{theta_yield:.3f}%`)")
    if theta_yield < 0.05:
        lines.append("   ⚠️ **資金利用率過低:** Theta 收益率未達 0.05%，可尋找高 VRP 標的建倉。")
    elif theta_yield > 0.30:
        lines.append("   ⚠️ **時間價值曝險過度:** Theta 收益率 > 0.30%，暗示承擔了極高的尾部風險。")
    else:
        lines.append("   ✅ **現金流健康:** 符合機構級 0.05% ~ 0.30% 之每日收租標準。")
    lines.append("")

    # 4. 資金熱度極限 (Portfolio Heat)
    portfolio_heat = (total_margin_used / user_capital) * 100 if user_capital > 0 else 0
    lines.append(f"🔹 **總保證金佔用 (Heat):** `${total_margin_used:,.2f}` (佔總資金 `{portfolio_heat:.1f}%`)")
    if portfolio_heat > 50.0:
        lines.append("   🚨 **爆倉警戒:** 資金熱度 > 50%！強烈建議停止建倉，保留現金防禦波動率擴張。")
    elif portfolio_heat > 30.0:
        lines.append("   ⚠️ **資金警戒:** 資金熱度 > 30%。已達常規滿水位，請嚴格審視新進場部位。")
    else:
        lines.append("   ✅ **資金水位健康:** 保留了充裕的流動性，可安全承擔新的高期望值部位。")
        
    return lines

def _analyze_correlation(positions_by_symbol):
    """
    計算板塊非系統性集中風險 (Correlation Matrix)
    """
    symbols = list(positions_by_symbol.keys())
    if len(symbols) <= 1:
        return []

    lines = ["", "🕸️ **【非系統性集中風險 (板塊連動性)】**", ""]
    try:
        hist_data = yf.download(symbols, period="60d", progress=False)['Close']
        if isinstance(hist_data, pd.Series):
            hist_data = hist_data.to_frame(name=symbols[0])
            
        returns = hist_data.pct_change().dropna()
        corr_matrix = returns.corr()

        high_corr_pairs = []
        for i in range(len(corr_matrix.columns)):
            for j in range(i+1, len(corr_matrix.columns)):
                rho = corr_matrix.iloc[i, j]
                if rho > 0.75:
                    high_corr_pairs.append((corr_matrix.columns[i], corr_matrix.columns[j], rho))

        lines.append(f"🔹 **板塊相關性掃描:** 目標 `{len(symbols)}` 檔 (60 日 Pearson 係數)")
        if high_corr_pairs:
            lines.append("   🚨 **高度正相關警告:** 發現板塊重疊曝險！")
            for sym1, sym2, rho in high_corr_pairs:
                lines.append(f"      ⚠️ `{sym1}` & `{sym2}` (ρ = {rho:.2f})")
            lines.append("   👉 **經理人建議:** 若發生整體利空，將引發 Gamma 同步擴張，建議適度降載。")
        else:
            lines.append("   ✅ **分散性良好:** 未發現 ρ > 0.75 的重疊曝險，非系統性風險受控。")
        lines.append("")
    except Exception as e:
        print(f"相關性矩陣運算失敗: {e}")
        lines.append("🔹 **板塊相關性掃描:** 無法完成")
        lines.append(f"   ⚠️ **運算失敗:** {e}")
        lines.append("")
        
    return lines

def check_portfolio_status_logic(portfolio_rows, user_capital=50000.0):
    """
    [Facade] 盤後動態結算與風險管線編排者 (Orchestrator)
    """
    if not portfolio_rows:
        return []

    report_lines = []
    today = datetime.now().date()
    
    total_portfolio_beta_delta = 0.0
    total_portfolio_theta = 0.0
    total_margin_used = 0.0  
    total_portfolio_gamma = 0.0 # 🔥 新增：追蹤投資組合總 Gamma

    try:
        spy_price = yf.Ticker("SPY").history(period="1d")['Close'].iloc[-1]
    except Exception:
        spy_price = 500.0 

    positions_by_symbol = {}
    for row in portfolio_rows:
        positions_by_symbol.setdefault(row[0], []).append(row)

    for symbol, rows in positions_by_symbol.items():
        try:
            ticker = yf.Ticker(symbol)
            hist = ticker.history(period="1d")
            if hist.empty: continue
            current_stock_price = hist['Close'].iloc[-1]
            beta = ticker.info.get('beta', 1.0) or 1.0

            # 🔥 抓取股息殖利率 q
            dividend_yield = ticker.info.get('dividendYield', 0.0)
            if dividend_yield is None: dividend_yield = 0.0

            option_chains_cache = {}

            for row in rows:
                _, opt_type, strike, expiry, entry_price, quantity, is_covered = row
                
                if expiry not in option_chains_cache:
                    option_chains_cache[expiry] = ticker.option_chain(expiry)
                
                chain_data = option_chains_cache[expiry].calls if opt_type == "call" else option_chains_cache[expiry].puts
                contract = chain_data[chain_data['strike'] == strike]
                if contract.empty: continue
                
                current_price = contract['lastPrice'].iloc[0]
                iv = contract['impliedVolatility'].iloc[0]
                
                exp_date = datetime.strptime(expiry, '%Y-%m-%d').date()
                dte = (exp_date - today).days
                t_years = max(dte, 1) / 365.0 
                
                # 計算 Greeks
                flag = 'c' if opt_type == 'call' else 'p'
                try:
                    current_delta = delta(flag, current_stock_price, strike, t_years, RISK_FREE_RATE, iv, dividend_yield)
                    daily_theta = theta(flag, current_stock_price, strike, t_years, RISK_FREE_RATE, iv, dividend_yield)
                    current_gamma = gamma(flag, current_stock_price, strike, t_years, RISK_FREE_RATE, iv, dividend_yield)
                except Exception:
                    current_delta, daily_theta, current_gamma = 0.0, 0.0, 0.0

                #保證金佔用累加 (區分 Naked Call 與 Covered Call)
                if quantity < 0:
                    if opt_type == 'call' and is_covered:
                        # 掩護性買權 (Covered Call)：保證金 = 持有 100 股現股的市值
                        margin_locked = current_stock_price * 100 * abs(quantity)
                    elif opt_type == 'call':
                        # 裸賣買權 (Naked Call)：Reg T 粗估公式
                        otm_amount = max(0, strike - current_stock_price)
                        margin_per_contract = max((0.20 * current_stock_price) - otm_amount + current_price, 0.10 * current_stock_price + current_price)
                        margin_locked = margin_per_contract * 100 * abs(quantity)
                    else:
                        # 現金擔保賣權 (Cash-Secured Put)
                        margin_locked = strike * 100 * abs(quantity)
                        
                    total_margin_used += margin_locked

                # 宏觀數據 Beta-Weighting 縮放 (轉換為 SPY 等效股數)
                position_delta = current_delta * quantity * 100
                spx_weighted_delta = position_delta * beta * (current_stock_price / spy_price)
                total_portfolio_beta_delta += spx_weighted_delta
                
                position_theta = daily_theta * quantity * 100
                total_portfolio_theta += position_theta
                
                # 🔥 Gamma 累加：賣方 (quantity < 0) 會產生負 Gamma
                position_gamma = current_gamma * quantity * 100
                # 🔥 修正 Gamma 加權公式：Gamma 是二階導數，必須對 (Beta * S/S_spy) 進行平方加權
                weighting_factor = beta * (current_stock_price / spy_price)
                spx_weighted_gamma = position_gamma * (weighting_factor ** 2)
                total_portfolio_gamma += spx_weighted_gamma

                # 防禦決策樹判定
                if entry_price > 0:
                    pnl_pct = (entry_price - current_price) / entry_price if quantity < 0 else (current_price - entry_price) / entry_price
                else:
                    pnl_pct = 0.0
                status = _evaluate_defense_status(quantity, opt_type, pnl_pct, current_delta, dte)

                # 生成單筆報告
                pnl_icon = "🟢" if pnl_pct > 0 else "🔴" if pnl_pct < 0 else "⚪"
                cc_tag = " 🛡️(CC)" if (opt_type == 'call' and is_covered) else ""
                line = (f"🔹 **{symbol}** ｜ `{expiry}` ｜ `${strike}` **{opt_type.upper()}**{cc_tag}\n"
                        f"├─ 💰 成本: `${entry_price:.2f}` ｜ 📈 現價: `${current_price:.2f}`\n"
                        f"├─ {pnl_icon} 損益: **{pnl_pct*100:+.2f}%**\n"
                        f"├─ ⏳ DTE: `{dte}` 天 ｜ ⚖️ SPY Δ: `{spx_weighted_delta:+.2f}`\n"
                        f"└─ 🎯 動作: {status}\n")
                report_lines.append(line)
        except Exception as e:
            print(f"處理 Symbol {symbol} 發生錯誤: {e}")
            continue

    # 組合尾部風險報告 (將 total_portfolio_gamma 傳入)
    report_lines.extend(_calculate_macro_risk(total_portfolio_beta_delta, total_portfolio_theta, total_margin_used, total_portfolio_gamma, user_capital))
    report_lines.extend(_analyze_correlation(positions_by_symbol))

    return report_lines