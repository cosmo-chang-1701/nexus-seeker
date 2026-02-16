import yfinance as yf
import pandas as pd
import pandas_ta as ta
import numpy as np
from datetime import datetime
from py_vollib.black_scholes.greeks.analytical import delta
from config import RISK_FREE_RATE, TARGET_DELTAS

def calculate_contract_delta(row, current_price, t_years, flag):
    """計算單一選擇權合約的理論 Delta 值"""
    iv = row['impliedVolatility']
    if pd.isna(iv) or iv <= 0.01: return 0.0 
    try:
        return delta(flag, current_price, row['strike'], t_years, RISK_FREE_RATE, iv)
    except Exception:
        return 0.0

def get_next_earnings_date(symbol):
    """取得下一次財報發布日期"""
    try:
        ticker = yf.Ticker(symbol)
        cal = ticker.calendar
        if cal is not None and not cal.empty and 'Earnings Date' in cal:
            earning_dates = cal['Earnings Date']
            if len(earning_dates) > 0:
                next_date = earning_dates[0]
                return next_date.date() if hasattr(next_date, 'date') else next_date
    except Exception:
        pass
    return None

def analyze_symbol(symbol):
    """掃描技術指標、波動率位階、期限結構，並過濾出最高期望值的選擇權合約"""
    try:
        ticker = yf.Ticker(symbol)
        # 提取 1 年歷史資料以計算 252 交易日的波動率位階
        df = ticker.history(period="1y")
        if df.empty or len(df) < 50: return None

        # ==========================================
        # 量化運算 1: 歷史波動率位階 (HV Rank)
        # ==========================================
        df['Log_Ret'] = np.log(df['Close'] / df['Close'].shift(1))
        df['HV_20'] = df['Log_Ret'].rolling(window=20).std() * np.sqrt(252)
        
        hv_min = df['HV_20'].min()
        hv_max = df['HV_20'].max()
        hv_current = df['HV_20'].iloc[-1]
        
        if hv_max > hv_min:
            hv_rank = ((hv_current - hv_min) / (hv_max - hv_min)) * 100
        else:
            hv_rank = 0.0

        # ==========================================
        # 量化運算 2: 價格技術指標
        # ==========================================
        df.ta.rsi(length=14, append=True)
        df.ta.sma(length=20, append=True)
        df.ta.macd(fast=12, slow=26, signal=9, append=True)
        
        latest = df.iloc[-1]
        price = latest['Close']
        rsi = latest['RSI_14']
        sma20 = latest['SMA_20']
        macd_hist = latest['MACDh_12_26_9']

        strategy, opt_type, target_delta, min_dte, max_dte = None, None, 0, 0, 0
        
        # ==========================================
        # 策略決策樹 (結合 HVR 波動率濾網)
        # ==========================================
        if rsi < 35 and hv_rank >= 30:
            strategy, opt_type, target_delta, min_dte, max_dte = "STO_PUT", "put", TARGET_DELTAS["STO_PUT"], 30, 45
        elif rsi > 65 and hv_rank >= 30:
            strategy, opt_type, target_delta, min_dte, max_dte = "STO_CALL", "call", TARGET_DELTAS["STO_CALL"], 30, 45
        elif price > sma20 and 50 <= rsi <= 65 and macd_hist > 0:
            strategy, opt_type, target_delta, min_dte, max_dte = "BTO_CALL", "call", TARGET_DELTAS["BTO_CALL"], 14, 30
        elif price < sma20 and 35 <= rsi <= 50 and macd_hist < 0:
            strategy, opt_type, target_delta, min_dte, max_dte = "BTO_PUT", "put", TARGET_DELTAS["BTO_PUT"], 14, 30
        else:
            return None # 不符合嚴格的建倉條件

        expirations = ticker.options
        if not expirations: return None
        today = datetime.now().date()

        # ==========================================
        # 量化運算 3: 波動率期限結構 (Term Structure)
        # ==========================================
        front_date, back_date = None, None
        front_diff, back_diff = 9999, 9999
        
        # 尋找最接近 30D 與 60D 的合約
        for exp in expirations:
            exp_date = datetime.strptime(exp, '%Y-%m-%d').date()
            dte = (exp_date - today).days
            if abs(dte - 30) < front_diff:
                front_diff = abs(dte - 30)
                front_date = exp
            if abs(dte - 60) < back_diff:
                back_diff = abs(dte - 60)
                back_date = exp
                
        ts_ratio = 1.0
        ts_state = "平滑 (Flat)"
        
        if front_date and back_date and front_date != back_date:
            try:
                # 抓取 Put 報價表來評估市場下行恐慌情緒
                front_chain = ticker.option_chain(front_date).puts
                back_chain = ticker.option_chain(back_date).puts
                
                # 抓取最接近現價的價平 (ATM) 合約
                front_atm = front_chain.iloc[(front_chain['strike'] - price).abs().argsort()[:1]]
                back_atm = back_chain.iloc[(back_chain['strike'] - price).abs().argsort()[:1]]
                
                front_iv = front_atm['impliedVolatility'].values[0]
                back_iv = back_atm['impliedVolatility'].values[0]
                
                if back_iv > 0.01:
                    ts_ratio = front_iv / back_iv
                    
                if ts_ratio >= 1.05:
                    ts_state = "🚨 恐慌 (Backwardation)"
                elif ts_ratio <= 0.95:
                    ts_state = "🌊 正常 (Contango)"
            except Exception:
                pass # 若報價表異常，則保持 Flat 預設值

        # ==========================================
        # 量化運算 4: Greeks 精算與最佳合約尋標
        # ==========================================
        target_date = None
        days_to_expiry = 0
        for exp in expirations:
            exp_date = datetime.strptime(exp, '%Y-%m-%d').date()
            days_to_expiry = (exp_date - today).days
            if min_dte <= days_to_expiry <= max_dte:
                target_date = exp
                break
                
        if not target_date: return None

        opt_chain = ticker.option_chain(target_date)
        chain_data = opt_chain.calls if opt_type == "call" else opt_chain.puts
        chain_data = chain_data[chain_data['volume'] > 0].copy() # 過濾無流動性合約
        if chain_data.empty: return None

        flag = 'c' if opt_type == "call" else 'p'
        t_years = max(days_to_expiry, 1) / 365.0
        
        chain_data['bs_delta'] = chain_data.apply(lambda row: calculate_contract_delta(row, price, t_years, flag), axis=1)
        chain_data = chain_data[chain_data['bs_delta'] != 0.0].copy()
        if chain_data.empty: return None

        # 找出 Delta 最接近目標值的合約
        chain_data['delta_diff'] = abs(chain_data['bs_delta'] - target_delta)
        best_contract = chain_data.sort_values('delta_diff').iloc[0]

        # ==========================================
        # 量化運算 5: AROC 資金效率濾網 (僅賣方)
        # ==========================================
        bid_price = best_contract['bid']
        strike_price = best_contract['strike']
        aroc = 0.0
        
        if strategy in ["STO_PUT", "STO_CALL"]:
            if bid_price <= 0: return None
            
            # Cash-Secured 資金佔用: 履約價 - 收取的權利金
            margin_required = strike_price - bid_price
            if margin_required <= 0: return None
            
            aroc = (bid_price / margin_required) * (365.0 / max(days_to_expiry, 1)) * 100
            
            # 拒絕資金效率低於 15% 的交易
            if aroc < 15.0:
                print(f"[{symbol}] 剔除: AROC {aroc:.1f}% 過低 (門檻 15%)")
                return None

        return {
            "symbol": symbol, "price": price, "rsi": rsi, "sma20": sma20,
            "hv_rank": hv_rank, "ts_ratio": ts_ratio, "ts_state": ts_state, 
            "strategy": strategy, "target_date": target_date, "dte": days_to_expiry, 
            "strike": strike_price, "bid": bid_price, "ask": best_contract['ask'], 
            "delta": best_contract['bs_delta'], "iv": best_contract['impliedVolatility'],
            "aroc": aroc
        }
    except Exception as e:
        print(f"分析 {symbol} 錯誤: {e}")
        return None

def check_portfolio_status_logic(portfolio_rows):
    """盤後動態結算與 Greeks 風險防禦引擎"""
    report_lines = []
    today = datetime.now().date()

    for row in portfolio_rows:
        # DB 傳入格式: (symbol, opt_type, strike, expiry, entry_price, quantity)
        symbol, opt_type, strike, expiry, entry_price, quantity = row

        try:
            ticker = yf.Ticker(symbol)
            # 獲取標的現價
            current_stock_price = ticker.history(period="1d")['Close'].iloc[-1]
            
            # 獲取持倉到期日的選擇權報價表
            opt_chain = ticker.option_chain(expiry)
            chain_data = opt_chain.calls if opt_type == "call" else opt_chain.puts
            
            # 定位持倉的特定履約價合約
            contract = chain_data[chain_data['strike'] == strike]
            if contract.empty:
                continue
            
            current_price = contract['lastPrice'].iloc[0]
            iv = contract['impliedVolatility'].iloc[0]
            
            # 準備 Greeks 運算參數
            exp_date = datetime.strptime(expiry, '%Y-%m-%d').date()
            dte = (exp_date - today).days
            t_years = max(dte, 1) / 365.0 
            
            # ==========================================
            # Greeks 動態精算 (評估當下即時曝險)
            # ==========================================
            flag = 'c' if opt_type == 'call' else 'p'
            try:
                current_delta = delta(flag, current_stock_price, strike, t_years, RISK_FREE_RATE, iv)
            except Exception:
                current_delta = 0.0

            # ==========================================
            # 動態防禦決策樹 (Dynamic Rolling Protocol)
            # ==========================================
            status = "⏳ 繼續持有"
            
            if quantity < 0: 
                # 賣方防禦邏輯 (Short Premium)
                pnl_pct = (entry_price - current_price) / entry_price
                
                if pnl_pct >= 0.50:
                    status = "✅ 建議停利 (獲利達 50%) - Buy to Close"
                elif pnl_pct <= -1.50:
                    status = "☠️ 黑天鵝警戒 (虧損達 150%) - 強制停損"
                # Delta 擴張防禦：防止 Gamma 爆炸
                elif opt_type == 'put' and current_delta <= -0.40:
                    status = "🚨 動態轉倉 (Delta 擴張) - 執行 Roll Down and Out"
                elif opt_type == 'call' and current_delta >= 0.40:
                    status = "🚨 動態轉倉 (Delta 擴張) - 執行 Roll Up and Out"
                # 靜態期限防禦
                elif dte <= 14 and pnl_pct < 0:
                    status = "⚠️ 期限防禦 (DTE < 14) - 迴避 Gamma 爆發，建議轉倉"
            else:
                # 買方防禦邏輯 (Long Premium)
                pnl_pct = (current_price - entry_price) / entry_price
                
                if pnl_pct >= 1.0:
                    status = "✅ 建議停利 (獲利達 100%) - Sell to Close"
                elif dte <= 14:
                    status = "🚨 動能衰竭 (DTE < 14) - 建議平倉保留殘值"
                elif pnl_pct <= -0.50:
                    status = "⚠️ 停損警戒 (本金回撤 50%)"

            line = (f"**{symbol}** {expiry} ${strike} {opt_type.upper()}\n"
                    f"└ 成本: `${entry_price:.2f}` | 現價: `${current_price:.2f}` | 損益: `{pnl_pct*100:+.1f}%`\n"
                    f"└ DTE: `{dte}` 天 | 當前 Delta: `{current_delta:.3f}`\n"
                    f"└ 動作: {status}")
            report_lines.append(line)

        except Exception as e:
            print(f"盤後結算 {symbol} 錯誤: {e}")
            continue

    return report_lines