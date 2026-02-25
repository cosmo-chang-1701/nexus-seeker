import yfinance as yf
import numpy as np
import pandas as pd
from datetime import datetime
from py_vollib.black_scholes_merton.greeks.analytical import delta, theta, gamma
from config import RISK_FREE_RATE
import logging
import math

# 設定 Logger
logger = logging.getLogger(__name__)

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

def _calculate_macro_risk(total_beta_delta, total_theta, total_margin_used, total_gamma, user_capital, spy_price=500.0):
    """
    計算投資組合的宏觀系統性風險，改用資金權重比例 (Exposure %) 判定。
    
    參數:
    - total_beta_delta: 總加權 Delta (等效 SPY 股數)
    - spy_price: 當前 SPY 價格 (用於計算總美元曝險)
    """
    lines = ["", "🌐 **【宏觀風險與資金水位報告】**", ""]
    
    # --- 1. 系統性方向風險 (Delta Exposure %) ---
    # 計算總美元曝險：等效股數 * SPY 單價
    net_exposure_dollars = total_beta_delta * spy_price
    
    # 計算曝險佔總資金比例
    exposure_pct = (net_exposure_dollars / user_capital) * 100 if user_capital > 0 else 0
    
    # 定義門檻 (例如：超過總資金的 15% 即視為過度曝險)
    DELTA_THRESHOLD_PCT = 15.0 
    
    if exposure_pct > DELTA_THRESHOLD_PCT:
        delta_status = f"🚨 **多頭曝險過高** (`{exposure_pct:.1f}%` > {DELTA_THRESHOLD_PCT}%)"
        advice = "   👉 建議：買入 SPY Put 或賣出 Call 對沖。"
    elif exposure_pct < -DELTA_THRESHOLD_PCT:
        delta_status = f"🚨 **空頭曝險過高** (`{abs(exposure_pct):.1f}%` > {DELTA_THRESHOLD_PCT}%)"
        advice = "   👉 建議：平倉空單或買入標普多單對沖。"
    else:
        delta_status = f"✅ **風險中性** (`{abs(exposure_pct):.1f}%` 內)"
        advice = "   👉 目前系統性風險受控。"

    lines.append(f"🔹 **淨 SPY Delta 曝險:** `${net_exposure_dollars:,.0f}` (等效 `{total_beta_delta:+.1f}` 股)")
    lines.append(f" └─ {delta_status}\n{advice}")
    lines.append("")

    # --- 2. 淨 Gamma 脆性評估 (同樣參數化) ---
    # Gamma 門檻：建議每 $10,000 資金容忍 2.0 單位 Gamma
    gamma_threshold = (user_capital / 10000.0) * 2.0
    
    if total_gamma < -gamma_threshold:
        gamma_status = "🚨 **脆性警告 (Fragile)**"
        g_msg = "   👉 下行加速度風險極大，建議注入正 Gamma。"
    elif total_gamma > gamma_threshold:
        gamma_status = "🛡️ **反脆弱 (Antifragile)**"
        g_msg = "   👉 波動越劇烈對帳戶越有利。"
    else:
        gamma_status = "✅ **Gamma 中性**"
        g_msg = "   👉 非線性風險受控。"

    lines.append(f"🔹 **組合淨 Gamma:** `{total_gamma:+.2f}`")
    lines.append(f" └─ {gamma_status}\n{g_msg}")
    lines.append("")

    # --- 3. Theta 收益率精算 (收租效率) ---
    theta_yield = (total_theta / user_capital) * 100 if user_capital > 0 else 0
    theta_status = "✅ 現金流健康"
    if theta_yield < 0.05:
        theta_status = "⚠️ **收益率過低** (資金閒置中，建議尋找高 VRP 標的)"
    elif theta_yield > 0.30:
        theta_status = "🔥 **過度收租** (小心爆倉！您正在承受極高的尾部風險)"
    
    lines.append(f"🔹 **每日預期 Theta:** `${total_theta:+.2f}` (`{theta_yield:.3f}%`)")
    lines.append(f" └─ {theta_status}")
    lines.append("")

    # --- 4. 資金熱度極限 (Portfolio Heat) ---
    portfolio_heat = (total_margin_used / user_capital) * 100 if user_capital > 0 else 0
    heat_status = "✅ 水位正常"
    if portfolio_heat > 50.0:
        heat_status = "🆘 **強制停止建倉** (隨時可能觸發保證金追繳)"
    elif portfolio_heat > 30.0:
        heat_status = "⚠️ **水位警戒** (已達常規上限，請嚴格執行止損)"
        
    lines.append(f"🔹 **資金熱度 (Heat):** `${total_margin_used:,.2f}` (`{portfolio_heat:.1f}%`)")
    lines.append(f" └─ {heat_status}")
        
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

def simulate_exposure_impact(current_total_delta, new_trade_data, user_capital, spy_price, suggested_contracts=1):
    """
    模擬成交後的總曝險變化。
    """
    # 1. 計算新交易帶來的總加權 Delta
    # 注意：analyze_symbol 回傳的 weighted_delta 是單口合約的 SPY 等效股數
    strategy = new_trade_data.get('strategy', '')
    side_multiplier = -1 if "STO" in strategy else 1
    new_trade_weighted_delta = new_trade_data.get('weighted_delta', 0.0) * side_multiplier * suggested_contracts
    
    # 2. 計算成交後的預期總 Delta
    projected_total_delta = current_total_delta + new_trade_weighted_delta
    
    # 3. 換算為預期美元曝險與百分比
    projected_exposure_dollars = projected_total_delta * spy_price
    projected_exposure_pct = (projected_exposure_dollars / user_capital) * 100 if user_capital > 0 else 0
    
    return projected_total_delta, projected_exposure_pct

def calculate_beta(df_stock, df_spy):
    """
    計算標的與基準 (SPY) 的相關性係數 (Beta)。
    公式: \beta = \frac{Cov(R_i, R_m)}{Var(R_m)}
    """
    try:
        # 對齊日期並清理缺失值
        combined = pd.concat([df_stock['Close'], df_spy['Close']], axis=1, keys=['stock', 'spy']).dropna()
        
        # 樣本數過少則回傳 1.0 (中性風險)
        if len(combined) < 60:
            return 1.0
            
        # 計算日收益率 (Daily Returns)
        returns = combined.pct_change().dropna()
        
        # 計算協方差矩陣 (Covariance Matrix)
        cov_matrix = np.cov(returns['stock'], returns['spy'])
        covariance = cov_matrix[0, 1]
        variance = cov_matrix[1, 1]
        
        beta = covariance / variance
        return round(float(beta), 2)
    except Exception:
        return 1.0

def check_portfolio_status_logic(portfolio_rows, user_capital=50000.0):
    """
    [Facade] 盤後動態結算與風險管線編排者 (Orchestrator)
    整合了 ETF 404 防護、Beta-Weighted Greeks 與二階風險評估。
    """
    if not portfolio_rows:
        return []

    report_lines = []
    today = datetime.now().date()
    
    total_portfolio_beta_delta = 0.0
    total_portfolio_theta = 0.0
    total_margin_used = 0.0  
    total_portfolio_gamma = 0.0 

    # 🚀 優化 1：批次下載歷史資料 (提高 Beta 計算精確度與速度)
    unique_symbols = sorted(list(set([row[0] for row in portfolio_rows])))
    all_targets = unique_symbols + ["SPY"]
    
    spy_hist = pd.DataFrame()
    spy_price = 500.0
    stock_hist_map = {}
    
    try:
        # 下載 90 天資料以供 Beta 計算 (僅取 Close 價格以節省流量)
        hists = yf.download(all_targets, period="90d", progress=False)
        if not hists.empty:
            # 取得 SPY 基準
            if "SPY" in hists['Close']:
                spy_series = hists['Close']['SPY']
                spy_hist = pd.DataFrame({'Close': spy_series})
                spy_price = spy_series.iloc[-1]
            
            # 將其他標的存入 Map
            for sym in unique_symbols:
                if sym in hists['Close']:
                    stock_hist_map[sym] = pd.DataFrame({'Close': hists['Close'][sym]})
    except Exception as e:
        logger.warning(f"批次歷史資料下載失敗: {e}")

    # 依照標的分群處理
    positions_by_symbol = {}
    for row in portfolio_rows:
        positions_by_symbol.setdefault(row[0], []).append(row)

    for symbol, rows in positions_by_symbol.items():
        try:
            ticker = yf.Ticker(symbol)
            stock_hist = stock_hist_map.get(symbol, pd.DataFrame())

            # 🚀 優化 2：使用 fast_info 避開 ETF Fundamentals 404 報錯
            try:
                f_info = ticker.fast_info
                current_stock_price = f_info.get('last_price') or (stock_hist['Close'].iloc[-1] if not stock_hist.empty else ticker.history(period="1d")['Close'].iloc[-1])
                is_etf = f_info.get('quoteType') == 'ETF'
                
                # 取得股息率 q (BSM 引擎校正用)
                dividend_yield = 0.015 if is_etf else (f_info.get('dividendYield', 0.0) or 0.0)
                
                # 精確計算 Beta (取代 ticker.info 靜態值)
                if not spy_hist.empty and not stock_hist.empty:
                    beta = calculate_beta(stock_hist, spy_hist)
                else:
                    beta = ticker.info.get('beta', 1.0) if not is_etf else 1.0
            except:
                # Fallback 邏輯
                current_stock_price = stock_hist['Close'].iloc[-1] if not stock_hist.empty else ticker.history(period="1d")['Close'].iloc[-1]
                dividend_yield, beta = 0.0, 1.0

            option_chains_cache = {}

            for row in rows:
                _, opt_type, strike, expiry, entry_price, quantity, stock_cost = row
                
                # 避免重複拉取同標的、同到期日的 Chain
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
                
                # 計算 Greeks (調用您現有的 BSM 模組)
                flag = 'c' if opt_type == 'call' else 'p'
                try:
                    curr_delta = delta(flag, current_stock_price, strike, t_years, RISK_FREE_RATE, iv, dividend_yield)
                    curr_theta = theta(flag, current_stock_price, strike, t_years, RISK_FREE_RATE, iv, dividend_yield)
                    curr_gamma = gamma(flag, current_stock_price, strike, t_years, RISK_FREE_RATE, iv, dividend_yield)
                except:
                    curr_delta, curr_theta, curr_gamma = 0.0, 0.0, 0.0

                # --- 保證金計算 ---
                if quantity < 0:
                    if opt_type == 'call' and stock_cost > 0.0:
                        margin_locked = 0.0 # Covered Call
                    elif opt_type == 'call':
                        otm = max(0, strike - current_stock_price)
                        margin_locked = max((0.20 * current_stock_price) - otm + current_price, 0.10 * current_stock_price + current_price) * 100 * abs(quantity)
                    else:
                        margin_locked = strike * 100 * abs(quantity) # CSP
                    total_margin_used += margin_locked

                # --- 🚀 宏觀風險聚合 (Beta-Weighting) ---
                weight_factor = beta * (current_stock_price / spy_price)
                
                # Delta 加權 (一階風險)
                pos_delta = curr_delta * quantity * 100
                spx_weighted_delta = pos_delta * weight_factor
                total_portfolio_beta_delta += spx_weighted_delta
                
                # Theta 累加 (時間價值收益)
                total_portfolio_theta += curr_theta * quantity * 100
                
                # Gamma 加權 (二階風險：平方加權確保非線性路徑一致)
                pos_gamma = curr_gamma * quantity * 100
                spx_weighted_gamma = pos_gamma * (weight_factor ** 2)
                total_portfolio_gamma += spx_weighted_gamma

                # --- 生成單筆報告內容 ---
                pnl_pct = (entry_price - current_price) / entry_price if quantity < 0 else (current_price - entry_price) / entry_price
                status = _evaluate_defense_status(quantity, opt_type, pnl_pct, curr_delta, dte)
                
                pnl_icon = "🟢" if pnl_pct > 0 else "🔴" if pnl_pct < 0 else "⚪"
                cc_tag = " 🛡️(CC)" if (opt_type == 'call' and stock_cost > 0.0) else ""
                
                report_lines.append(
                    f"🔹 **{symbol}** ｜ `{expiry}` ｜ `${strike}` **{opt_type.upper()}**{cc_tag}\n"
                    f"├─ 💰 成本: `${entry_price:.2f}` ｜ 📈 現價: `${current_price:.2f}`\n"
                    f"├─ {pnl_icon} 損益: **{pnl_pct*100:+.2f}%**\n"
                    f"├─ ⏳ DTE: `{dte}` 天 ｜ ⚖️ SPY Δ: `{spx_weighted_delta:+.2f}`\n"
                    f"└─ 🎯 動作: {status}\n"
                )
        except Exception as e:
            logger.error(f"Symbol {symbol} 處理失敗: {e}")
            continue

    # 🚀 整合最後的宏觀風險報告
    report_lines.extend(_calculate_macro_risk(total_portfolio_beta_delta, total_portfolio_theta, total_margin_used, total_portfolio_gamma, user_capital, spy_price))
    report_lines.extend(_analyze_correlation(positions_by_symbol))

    return report_lines

def optimize_position_risk(current_delta, unit_weighted_delta, user_capital, spy_price, risk_limit_pct=15.0, strategy=""):
    """
    計算符合風險紅線的安全成交口數與對沖建議。
    """
    # 1. 計算總資金允許的最大 SPY 等效股數絕對值 (Max Safe Shares)
    max_safe_shares = (user_capital * (risk_limit_pct / 100)) / spy_price
    
    # 2. 單口對帳戶部位的實質衝擊 (考慮策略方向)
    side_multiplier = -1 if "STO" in strategy else 1
    pos_impact_per_unit = unit_weighted_delta * side_multiplier
    
    # 3. 計算理論安全口數 (向下取整)
    safe_qty = 0
    if pos_impact_per_unit > 0:
        room = max_safe_shares - current_delta
        safe_qty = math.floor(room / pos_impact_per_unit) if room > 0 else 0
    elif pos_impact_per_unit < 0:
        room = -max_safe_shares - current_delta
        safe_qty = math.floor(room / pos_impact_per_unit) if room < 0 else 0

    safe_qty = max(0, safe_qty)
    
    # 4. 如果連 1 口都過不了，計算建議對沖股數
    suggested_hedge_spy = 0.0
    if safe_qty == 0:
        projected_delta = current_delta + pos_impact_per_unit
        if projected_delta > max_safe_shares:
            suggested_hedge_spy = projected_delta - max_safe_shares
        elif projected_delta < -max_safe_shares:
            suggested_hedge_spy = projected_delta - (-max_safe_shares) # 負值，代表需要買入 SPY 進行對沖
        
    return safe_qty, round(suggested_hedge_spy, 1)