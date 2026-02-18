import yfinance as yf
from datetime import datetime
from config import RISK_FREE_RATE
from .greeks import calculate_contract_delta
import pandas as pd # Needed for correlation matrix

def check_portfolio_status_logic(portfolio_rows):
    """盤後動態結算、Greeks 風險防禦，與投資組合 Beta 權重宏觀風險評估"""
    report_lines = []
    today = datetime.now().date()

    if not portfolio_rows:
        return report_lines

    # ==========================================
    # 🔥 宏觀風險準備：取得 SPY 基準價格
    # ==========================================
    try:
        spy_price = yf.Ticker("SPY").history(period="1d")['Close'].iloc[-1]
    except Exception:
        spy_price = 500.0  # 斷線時的防呆預設值

    total_portfolio_beta_delta = 0.0

    # 1. 依 Symbol 分組整理持倉
    positions_by_symbol = {}
    for row in portfolio_rows:
        symbol = row[0]
        positions_by_symbol.setdefault(symbol, []).append(row)

    # 2. 逐一 Symbol 處理
    for symbol, rows in positions_by_symbol.items():
        try:
            ticker = yf.Ticker(symbol)
            hist = ticker.history(period="1d")
            if hist.empty: continue
            current_stock_price = hist['Close'].iloc[-1]
            
            # 取得該股票相對於大盤的 Beta 值
            try:
                beta = ticker.info.get('beta', 1.0)
                if beta is None: beta = 1.0
            except:
                beta = 1.0
                
            option_chains_cache = {}

            for row in rows:
                _, opt_type, strike, expiry, entry_price, quantity = row
                
                try:
                    if expiry not in option_chains_cache:
                        option_chains_cache[expiry] = ticker.option_chain(expiry)
                    
                    opt_chain = option_chains_cache[expiry]
                    chain_data = opt_chain.calls if opt_type == "call" else opt_chain.puts
                    contract = chain_data[chain_data['strike'] == strike]
                    if contract.empty: continue
                    
                    current_price = contract['lastPrice'].iloc[0]
                    iv = contract['impliedVolatility'].iloc[0]
                    
                    exp_date = datetime.strptime(expiry, '%Y-%m-%d').date()
                    dte = (exp_date - today).days
                    t_years = max(dte, 1) / 365.0 
                    
                    # 計算單一合約的 Delta
                    flag = 'c' if opt_type == 'call' else 'p'
                    try:
                        current_delta = calculate_contract_delta({'impliedVolatility': iv, 'strike': strike}, current_stock_price, t_years, flag)
                    except Exception:
                        current_delta = 0.0

                    # ==========================================
                    # 🔥 投資組合宏觀風險精算 (Beta-Weighted Delta)
                    # ==========================================
                    # 1. 換算為部位總 Delta (留意賣方 quantity 為負數)
                    position_delta = current_delta * quantity * 100
                    
                    # 2. Beta 與價格權重縮放
                    beta_weight = beta * (current_stock_price / spy_price)
                    
                    # 3. 算出該部位等同於多少股 SPY 的 Delta
                    spx_weighted_delta = position_delta * beta_weight
                    total_portfolio_beta_delta += spx_weighted_delta

                    # 動態防禦決策樹
                    status = "⏳ 繼續持有"
                    
                    if quantity < 0: 
                        pnl_pct = (entry_price - current_price) / entry_price
                        if pnl_pct >= 0.50:
                            status = "✅ 建議停利 (獲利達 50%) - Buy to Close"
                        elif pnl_pct <= -1.50:
                            status = "☠️ 黑天鵝警戒 (虧損達 150%) - 強制停損"
                        elif opt_type == 'put' and current_delta <= -0.40:
                            status = "🚨 動態轉倉 (Delta 擴張) - 執行 Roll Down and Out"
                        elif opt_type == 'call' and current_delta >= 0.40:
                            status = "🚨 動態轉倉 (Delta 擴張) - 執行 Roll Up and Out"
                        elif dte <= 14 and pnl_pct < 0:
                            status = "⚠️ 期限防禦 (DTE < 14) - 迴避 Gamma 爆發，建議轉倉"
                    else:
                        pnl_pct = (current_price - entry_price) / entry_price
                        if pnl_pct >= 1.0:
                            status = "✅ 建議停利 (獲利達 100%) - Sell to Close"
                        elif dte <= 14:
                            status = "🚨 動能衰竭 (DTE < 14) - 建議平倉保留殘值"
                        elif pnl_pct <= -0.50:
                            status = "⚠️ 停損警戒 (本金回撤 50%)"

                    # 報告中加入等效 SPY Delta 的顯示
                    line = (f"**{symbol}** {expiry} ${strike} {opt_type.upper()}\n"
                            f"└ 成本: `${entry_price:.2f}` | 現價: `${current_price:.2f}` | 損益: `{pnl_pct*100:+.1f}%`\n"
                            f"└ DTE: `{dte}` 天 | 原始 Delta: `{current_delta:.3f}` | SPY 等效 Delta: `{spx_weighted_delta:+.1f}`\n"
                            f"└ 動作: {status}")
                    report_lines.append(line)

                except Exception as inner_e:
                    print(f"處理持倉 {symbol} {expiry} 錯誤: {inner_e}")
        
        except Exception as e:
            print(f"處理 Symbol {symbol} 發生總體錯誤: {e}")
            continue

    # ==========================================
    # 宏觀風險診斷報告 (附加於列表最下方)
    # ==========================================
    if report_lines:
        report_lines.append("") # 空行分隔
        report_lines.append("🌐 **【宏觀系統性風險評估 (SPY Beta-Weighted)】**")
        report_lines.append(f"└ 投資組合淨 Delta: **`{total_portfolio_beta_delta:+.2f}`** (等同持有大盤股數)")
        
        # 避險邏輯判定 (設定閥值為 ±50 股 SPY 曝險)
        if total_portfolio_beta_delta > 50:
            advice = "🚨 **多頭曝險過高**：大盤若發生回調，您的部位將受重創。建議建立 SPY 避險空單 (如 BTO Put) 中和。"
        elif total_portfolio_beta_delta < -50:
            advice = "🚨 **空頭曝險過高**：大盤若發生強勢軋空，您的部位將面臨風險。建議建立大盤避險多單。"
        else:
            advice = "✅ **風險中性 (Delta Neutral)**：您的帳戶對大盤漲跌免疫力佳，受到系統性風險影響較小。"
            
        report_lines.append(f"└ 經理人建議: {advice}")

        # ==========================================
        # 投資組合相關性矩陣 (Correlation Matrix Risk)
        # ==========================================
        symbols = list(positions_by_symbol.keys())
        if len(symbols) > 1:
            report_lines.append("") 
            report_lines.append("🕸️ **【非系統性集中風險 (Idiosyncratic Concentration)】**")
            try:
                # 抓取 60 日歷史收盤價建立報酬率矩陣
                hist_data = yf.download(symbols, period="60d", progress=False)['Close']
                
                # yf.download 單一標的防呆機制
                if isinstance(hist_data, pd.Series):
                    hist_data = hist_data.to_frame(name=symbols[0])
                
                # 計算日報酬率 (Percentage Change)
                returns = hist_data.pct_change().dropna()
                
                # 建立 Pearson 相關係數矩陣
                corr_matrix = returns.corr()

                high_corr_pairs = []
                # 遍歷對稱矩陣的上半部，尋找高度正相關配對
                for i in range(len(corr_matrix.columns)):
                    for j in range(i+1, len(corr_matrix.columns)):
                        sym1 = corr_matrix.columns[i]
                        sym2 = corr_matrix.columns[j]
                        rho = corr_matrix.iloc[i, j]
                        
                        # 閥值設定：ρ > 0.75 視為具備高度板塊連動性
                        if rho > 0.75:
                            high_corr_pairs.append((sym1, sym2, rho))

                report_lines.append(f"└ 掃描 {len(symbols)} 檔標的之 60 日 Pearson 相關係數")
                
                if high_corr_pairs:
                    report_lines.append("🚨 **警告：發現高度正相關板塊重疊**")
                    for sym1, sym2, rho in high_corr_pairs:
                        report_lines.append(f"   ⚠️ `{sym1}` & `{sym2}`: 相關係數 `ρ = {rho:.2f}`")
                    report_lines.append("   👉 經理人建議: 若板塊發生利空，此類部位將發生 Gamma 同步擴張，建議平倉或轉倉降載。")
                else:
                    report_lines.append("✅ **分散性良好**：未發現相關係數 ρ > 0.75 的重疊曝險，板塊防禦力佳。")

            except Exception as e:
                print(f"相關性矩陣運算失敗: {e}")

    return report_lines
