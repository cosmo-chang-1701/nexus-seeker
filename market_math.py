import yfinance as yf
import pandas as pd
import pandas_ta as ta
from datetime import datetime
from py_vollib.black_scholes.greeks.analytical import delta
from config import RISK_FREE_RATE, TARGET_DELTAS

def calculate_contract_delta(row, current_price, t_years, flag):
    iv = row['impliedVolatility']
    if pd.isna(iv) or iv <= 0.01: return 0.0 
    try:
        return delta(flag, current_price, row['strike'], t_years, RISK_FREE_RATE, iv)
    except Exception:
        return 0.0

def get_next_earnings_date(symbol):
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
    """掃描技術指標並過濾最佳選擇權合約"""
    try:
        ticker = yf.Ticker(symbol)
        df = ticker.history(period="6mo")
        if df.empty: return None

        df.ta.rsi(length=14, append=True)
        df.ta.sma(length=20, append=True)
        df.ta.macd(fast=12, slow=26, signal=9, append=True)
        
        latest = df.iloc[-1]
        price = latest['Close']
        rsi = latest['RSI_14']
        sma20 = latest['SMA_20']
        macd_hist = latest['MACDh_12_26_9']

        strategy, opt_type, target_delta, min_dte, max_dte = None, None, 0, 0, 0
        
        if rsi < 35:
            strategy, opt_type, target_delta, min_dte, max_dte = "STO_PUT", "put", TARGET_DELTAS["STO_PUT"], 30, 45
        elif rsi > 65:
            strategy, opt_type, target_delta, min_dte, max_dte = "STO_CALL", "call", TARGET_DELTAS["STO_CALL"], 30, 45
        elif price > sma20 and 50 <= rsi <= 65 and macd_hist > 0:
            strategy, opt_type, target_delta, min_dte, max_dte = "BTO_CALL", "call", TARGET_DELTAS["BTO_CALL"], 14, 30
        elif price < sma20 and 35 <= rsi <= 50 and macd_hist < 0:
            strategy, opt_type, target_delta, min_dte, max_dte = "BTO_PUT", "put", TARGET_DELTAS["BTO_PUT"], 14, 30
        else:
            return None

        expirations = ticker.options
        if not expirations: return None
        
        target_date = None
        today = datetime.now().date()
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
        chain_data = chain_data[chain_data['volume'] > 0].copy()
        if chain_data.empty: return None

        flag = 'c' if opt_type == "call" else 'p'
        t_years = days_to_expiry / 365.0
        
        chain_data['bs_delta'] = chain_data.apply(lambda row: calculate_contract_delta(row, price, t_years, flag), axis=1)
        chain_data = chain_data[chain_data['bs_delta'] != 0.0].copy()
        if chain_data.empty: return None

        chain_data['delta_diff'] = abs(chain_data['bs_delta'] - target_delta)
        best_contract = chain_data.sort_values('delta_diff').iloc[0]

        return {
            "symbol": symbol, "price": price, "rsi": rsi, "sma20": sma20,
            "strategy": strategy, "target_date": target_date, "dte": days_to_expiry,
            "strike": best_contract['strike'], "bid": best_contract['bid'], 
            "ask": best_contract['ask'], "delta": best_contract['bs_delta'], 
            "iv": best_contract['impliedVolatility']
        }
    except Exception as e:
        print(f"分析 {symbol} 錯誤: {e}")
        return None

def check_portfolio_status_logic(portfolio_rows):
    """結算盤後庫存損益狀態"""
    report_lines = []
    today = datetime.now().date()
    
    for row in portfolio_rows:
        trade_id, symbol, opt_type, strike, expiry, entry_price, quantity = row
        try:
            exp_date = datetime.strptime(expiry, '%Y-%m-%d').date()
            dte = (exp_date - today).days
            
            ticker = yf.Ticker(symbol)
            opt_chain = ticker.option_chain(expiry)
            chain_data = opt_chain.calls if opt_type == "call" else opt_chain.puts
            contract = chain_data[chain_data['strike'] == strike]
            
            if contract.empty:
                report_lines.append(f"⚠️ `{symbol}`: 找不到 {expiry} 到期、履約價 {strike} 的合約。")
                continue
                
            current_price = contract.iloc[0]['lastPrice']
            
            if quantity < 0: # 賣方邏輯
                profit_pct = (entry_price - current_price) / entry_price
                action = "⏳ 繼續持有"
                
                if profit_pct >= 0.50:
                    action = "✅ **建議停利 (獲利 50%)** - Buy to Close"
                elif dte <= 14 and profit_pct < 0:
                    action = "🚨 **建議轉倉 (防禦)** - DTE 過低且虧損"
                elif current_price >= (entry_price * 2.5):
                    action = "☠️ **建議停損 (虧損 150%)** - 防禦"

                sign = "+" if profit_pct > 0 else ""
                report_lines.append(
                    f"**{symbol}** {expiry} ${strike} {opt_type.upper()}\n"
                    f"└ 成本: `${entry_price}` | 現價: `${current_price:.2f}` | 損益: `{sign}{profit_pct:.1%}`\n"
                    f"└ DTE: `{dte}` 天 | 動作: {action}\n"
                )
        except Exception as e:
            report_lines.append(f"❌ 分析 `{symbol}` 發生錯誤: {e}")
            
    return report_lines