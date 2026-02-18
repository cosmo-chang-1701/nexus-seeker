import math
import pandas as pd
import pandas_ta as ta
import numpy as np
import yfinance as yf
from datetime import datetime
from config import TARGET_DELTAS
from .greeks import calculate_contract_delta
from .data import get_next_earnings_date

def _calculate_technical_indicators(df):
    """計算技術指標與波動率位階"""
    try:
        if df.empty or len(df) < 50:
            return None
            
        df['Log_Ret'] = np.log(df['Close'] / df['Close'].shift(1))
        df['HV_20'] = df['Log_Ret'].rolling(window=20).std() * np.sqrt(252)
        hv_min = df['HV_20'].min()
        hv_max = df['HV_20'].max()
        hv_current = df['HV_20'].iloc[-1]
        hv_rank = ((hv_current - hv_min) / (hv_max - hv_min)) * 100 if hv_max > hv_min else 0.0

        df.ta.rsi(length=14, append=True)
        df.ta.sma(length=20, append=True)
        df.ta.macd(fast=12, slow=26, signal=9, append=True)
        
        latest = df.iloc[-1]
        return {
            'price': latest['Close'],
            'rsi': latest['RSI_14'],
            'sma20': latest['SMA_20'],
            'macd_hist': latest['MACDh_12_26_9'],
            'hv_current': hv_current,
            'hv_rank': hv_rank
        }
    except Exception as e:
        print(f"指標計算錯誤: {e}")
        return None

def _determine_strategy_signal(indicators):
    """根據技術指標決定策略"""
    price = indicators['price']
    rsi = indicators['rsi']
    hv_rank = indicators['hv_rank']
    sma20 = indicators['sma20']
    macd_hist = indicators['macd_hist']

    # 確保 TARGET_DELTAS 存在，避免 import 失敗或 key error
    deltas = TARGET_DELTAS if TARGET_DELTAS else {}

    if rsi < 35 and hv_rank >= 30:
        return "STO_PUT", "put", deltas.get("STO_PUT", -0.16), 30, 45
    elif rsi > 65 and hv_rank >= 30:
        return "STO_CALL", "call", deltas.get("STO_CALL", 0.16), 30, 45
    elif price > sma20 and 50 <= rsi <= 65 and macd_hist > 0:
        return "BTO_CALL", "call", deltas.get("BTO_CALL", 0.50), 14, 30
    elif price < sma20 and 35 <= rsi <= 50 and macd_hist < 0:
        return "BTO_PUT", "put", deltas.get("BTO_PUT", -0.50), 14, 30
    else:
        return None, None, 0, 0, 0

def _calculate_mmm(ticker, price, today, symbol):
    """計算財報日 MMM (Market Maker Move)"""
    earnings_date = get_next_earnings_date(ticker)
    days_to_earnings = -1
    mmm_pct, safe_lower, safe_upper = 0.0, 0.0, 0.0

    if earnings_date:
        if isinstance(earnings_date, datetime):
            earnings_date = earnings_date.date()
        days_to_earnings = (earnings_date - today).days
        
        if 0 <= days_to_earnings <= 14:
            target_exp_for_mmm = None
            try:
                for exp in ticker.options:
                    if datetime.strptime(exp, '%Y-%m-%d').date() >= earnings_date:
                        target_exp_for_mmm = exp
                        break
            except Exception:
                pass
            
            if target_exp_for_mmm:
                try:
                    chain_mmm = ticker.option_chain(target_exp_for_mmm)
                    
                    # Call Price
                    calls_mmm = chain_mmm.calls
                    atm_call_idx = (calls_mmm['strike'] - price).abs().argsort()[:1]
                    c_price = 0
                    if not atm_call_idx.empty:
                        atm_call = calls_mmm.iloc[atm_call_idx]
                        c_bid, c_ask, c_last = atm_call['bid'].values[0], atm_call['ask'].values[0], atm_call['lastPrice'].values[0]
                        c_price = (c_bid + c_ask)/2 if (c_bid > 0 and c_ask > 0) else c_last

                    # Put Price
                    puts_mmm = chain_mmm.puts
                    atm_put_idx = (puts_mmm['strike'] - price).abs().argsort()[:1]
                    p_price = 0
                    if not atm_put_idx.empty:
                        atm_put = puts_mmm.iloc[atm_put_idx]
                        p_bid, p_ask, p_last = atm_put['bid'].values[0], atm_put['ask'].values[0], atm_put['lastPrice'].values[0]
                        p_price = (p_bid + p_ask)/2 if (p_bid > 0 and p_ask > 0) else p_last
                    
                    if price > 0:
                        mmm_pct = ((c_price + p_price) / price) * 100
                        safe_lower = price * (1 - mmm_pct / 100)
                        safe_upper = price * (1 + mmm_pct / 100)
                except Exception as e:
                    print(f"[{symbol}] MMM 運算失敗: {e}")

    return mmm_pct, safe_lower, safe_upper, days_to_earnings

def _calculate_term_structure(ticker, expirations, price, today):
    """計算波動率期限結構"""
    front_date, back_date = None, None
    front_diff, back_diff = 9999, 9999
    
    for exp in expirations:
        dte_val = (datetime.strptime(exp, '%Y-%m-%d').date() - today).days
        if abs(dte_val - 30) < front_diff:
            front_diff, front_date = abs(dte_val - 30), exp
        if abs(dte_val - 60) < back_diff:
            back_diff, back_date = abs(dte_val - 60), exp
            
    ts_ratio, ts_state = 1.0, "平滑 (Flat)"
    if front_date and back_date and front_date != back_date:
        try:
            front_chain = ticker.option_chain(front_date).puts
            back_chain = ticker.option_chain(back_date).puts
            
            front_iv_idx = (front_chain['strike'] - price).abs().argsort()[:1]
            back_iv_idx = (back_chain['strike'] - price).abs().argsort()[:1]
            
            if not front_iv_idx.empty and not back_iv_idx.empty:
                front_iv = front_chain.iloc[front_iv_idx]['impliedVolatility'].values[0]
                back_iv = back_chain.iloc[back_iv_idx]['impliedVolatility'].values[0]
                
                if back_iv > 0.01:
                    ts_ratio = front_iv / back_iv
                
                if ts_ratio >= 1.05:
                    ts_state = "🚨 恐慌 (Backwardation)"
                elif ts_ratio <= 0.95:
                    ts_state = "🌊 正常 (Contango)"
        except Exception:
            pass
            
    return ts_ratio, ts_state

def _find_target_expiry(expirations, today, min_dte, max_dte):
    """尋找符合天數的到期日"""
    for exp in expirations:
        days_to_expiry = (datetime.strptime(exp, '%Y-%m-%d').date() - today).days
        if min_dte <= days_to_expiry <= max_dte:
            return exp, days_to_expiry
    return None, 0

def _get_best_contract_data(ticker, target_expiry_date, opt_type, target_delta, price, days_to_expiry):
    """取得最佳合約與 Greeks"""
    try:
        # 抓取年化股息殖利率 (Annual Dividend Yield)
        # 若為無配息股票 (如 TSLA)，yfinance 可能回傳 None，預設給 0.0
        dividend_yield = ticker.info.get('dividendYield', 0.0)
        if dividend_yield is None:
            dividend_yield = 0.0
    except:
        dividend_yield = 0.0

    try:
        opt_chain = ticker.option_chain(target_expiry_date)
        chain_data = opt_chain.calls if opt_type == "call" else opt_chain.puts
        chain_data = chain_data[chain_data['volume'] > 0].copy()
        
        if chain_data.empty:
            return None, None

        t_years = max(days_to_expiry, 1) / 365.0
        flag = 'c' if opt_type == "call" else 'p'
        chain_data['bs_delta'] = chain_data.apply(
            lambda row: calculate_contract_delta(row, price, t_years, flag, q=dividend_yield), 
            axis=1
        )
        chain_data = chain_data[chain_data['bs_delta'] != 0.0].copy()
        
        if chain_data.empty:
            return None, None

        best_contract = chain_data.loc[(chain_data['bs_delta'] - target_delta).abs().idxmin()]
        return best_contract, opt_chain # Return opt_chain for skew calc
    except Exception:
        return None, None

def _calculate_vertical_skew(opt_chain, price, days_to_expiry, strategy, symbol):
    """計算垂直波動率偏態"""
    vertical_skew = 1.0
    skew_state = "⚖️ 中性 (Neutral)"
    t_years = max(days_to_expiry, 1) / 365.0
    
    try:
        calls_skew = opt_chain.calls[opt_chain.calls['volume'] > 0].copy()
        puts_skew = opt_chain.puts[opt_chain.puts['volume'] > 0].copy()
        
        if not calls_skew.empty and not puts_skew.empty:
            calls_skew['bs_delta'] = calls_skew.apply(lambda row: calculate_contract_delta(row, price, t_years, 'c'), axis=1)
            puts_skew['bs_delta'] = puts_skew.apply(lambda row: calculate_contract_delta(row, price, t_years, 'p'), axis=1)
            
            call_25 = calls_skew.iloc[(calls_skew['bs_delta'] - 0.25).abs().argsort()[:1]]
            put_25 = puts_skew.iloc[(puts_skew['bs_delta'] - (-0.25)).abs().argsort()[:1]]
            
            if not call_25.empty and not put_25.empty:
                iv_call_25 = call_25['impliedVolatility'].values[0]
                iv_put_25 = put_25['impliedVolatility'].values[0]
                
                if iv_call_25 > 0.01:
                    vertical_skew = iv_put_25 / iv_call_25
                    
                if vertical_skew >= 1.30:
                    skew_state = "⚠️ 嚴重左偏 (高尾部風險)"
                    if strategy == "STO_PUT" and vertical_skew >= 1.50:
                        print(f"[{symbol}] 剔除: 垂直偏態比率 {vertical_skew:.2f} 過高，拒絕承接下行風險")
                        return None, None
                elif vertical_skew <= 0.90:
                    skew_state = "🚀 右偏 (看漲狂熱)"
    except Exception as e:
        print(f"[{symbol}] 垂直偏態運算錯誤: {e}")
        
    return vertical_skew, skew_state

def _validate_risk_and_liquidity(strategy, best_contract, price, hv_current, days_to_expiry, symbol):
    """驗證流動性、VRP 與 預期波動"""
    bid = best_contract['bid']
    ask = best_contract['ask']
    strike = best_contract['strike']
    iv = best_contract['impliedVolatility']
    
    # 1. 流動性
    if bid <= 0 or ask <= 0:
        print(f"[{symbol}] 剔除: 報價異常 (Bid或Ask <= 0)")
        return None
        
    spread = ask - bid
    mid_price = (ask + bid) / 2.0
    spread_ratio = (spread / mid_price) * 100 if mid_price > 0 else 999.0
    
    if spread > 0.20 and spread_ratio > 10.0:
        print(f"[{symbol}] 剔除: 流動性極差 (價差 ${spread:.2f}, 佔比 {spread_ratio:.1f}%)")
        return None
        
    # 2. VRP (僅賣方)
    vrp = iv - hv_current
    if strategy in ["STO_PUT", "STO_CALL"]:
        if vrp < 0:
            print(f"[{symbol}] 剔除: VRP {vrp*100:.2f}% < 0 (IV 被低估，無風險溢酬)")
            return None

    # 3. 預期波動 (Expected Move)
    expected_move = price * iv * math.sqrt(max(days_to_expiry, 1) / 365.0)
    em_lower = price - expected_move
    em_upper = price + expected_move
    
    if strategy == "STO_PUT":
        breakeven = strike - bid
        if breakeven > em_lower:
            print(f"[{symbol}] 剔除: 損益兩平點 ${breakeven:.2f} 落入 1σ 預期跌幅內 (安全下緣 ${em_lower:.2f})")
            return None
    elif strategy == "STO_CALL":
        breakeven = strike + bid
        if breakeven < em_upper:
            print(f"[{symbol}] 剔除: 損益兩平點 ${breakeven:.2f} 落入 1σ 預期漲幅內 (安全上緣 ${em_upper:.2f})")
            return None
            
    return {
        "bid": bid, "ask": ask, "spread": spread, "spread_ratio": spread_ratio,
        "vrp": vrp, "expected_move": expected_move, "em_lower": em_lower, "em_upper": em_upper
    }

def _calculate_sizing(strategy, best_contract, days_to_expiry):
    """計算資金效率與倉位大小"""
    aroc = 0.0
    alloc_pct = 0.0
    margin_per_contract = 0.0
    
    bid = best_contract['bid']
    strike = best_contract['strike']
    delta = best_contract['bs_delta']
    
    if strategy in ["STO_PUT", "STO_CALL"]:
        margin_required = strike - bid # 粗估
        if margin_required > 0:
            aroc = (bid / margin_required) * (365.0 / max(days_to_expiry, 1)) * 100
            
            if aroc >= 15.0:
                b = bid / margin_required
                p = 1.0 - abs(delta)
                if b > 0:
                    kelly_f = (p * (b + 1) - 1) / b
                    alloc_pct = min(max(kelly_f * 0.25, 0.0), 0.05)
                    margin_per_contract = margin_required * 100
                    
    return aroc, alloc_pct, margin_per_contract

def analyze_symbol(symbol):
    """
    掃描技術指標、波動率位階、期限結構與造市商預期波動，並過濾最佳合約。
    """
    try:
        ticker = yf.Ticker(symbol)
        df = ticker.history(period="1y")
        
        # 1. 技術指標
        indicators = _calculate_technical_indicators(df)
        if not indicators: return None
        price = indicators['price']

        # 2. 策略訊號
        strategy, opt_type, target_delta, min_dte, max_dte = _determine_strategy_signal(indicators)
        if not strategy: return None

        expirations = ticker.options
        if not expirations: return None
        today = datetime.now().date()

        # 3. 進階市場分析 (MMM, Term Structure)
        mmm_pct, safe_lower, safe_upper, days_to_earnings = _calculate_mmm(ticker, price, today, symbol)
        ts_ratio, ts_state = _calculate_term_structure(ticker, expirations, price, today)

        # 4. 合約篩選
        target_expiry_date, days_to_expiry = _find_target_expiry(expirations, today, min_dte, max_dte)
        if not target_expiry_date: return None

        best_contract, opt_chain = _get_best_contract_data(ticker, target_expiry_date, opt_type, target_delta, price, days_to_expiry)
        if best_contract is None: return None

        # 5. 垂直偏態分析
        if opt_chain:
            vertical_skew, skew_state = _calculate_vertical_skew(opt_chain, price, days_to_expiry, strategy, symbol)
            if vertical_skew is None: return None # Skew check failed (e.g. too risky)
        else:
             vertical_skew, skew_state = 1.0, "N/A"

        # 6. 風險與流動性驗證
        risk_metrics = _validate_risk_and_liquidity(strategy, best_contract, price, indicators['hv_current'], days_to_expiry, symbol)
        if not risk_metrics: return None

        # 7. 倉位計算
        aroc, alloc_pct, margin_per_contract = _calculate_sizing(strategy, best_contract, days_to_expiry)
        if strategy in ["STO_PUT", "STO_CALL"] and aroc < 15.0:
            return None

        # 8. 組合結果
        return {
            "symbol": symbol, "price": price, 
            "rsi": indicators['rsi'], "sma20": indicators['sma20'], "hv_rank": indicators['hv_rank'],
            "ts_ratio": ts_ratio, "ts_state": ts_state,
            "v_skew": vertical_skew, "v_skew_state": skew_state,
            "earnings_days": days_to_earnings, "mmm_pct": mmm_pct,
            "safe_lower": safe_lower, "safe_upper": safe_upper,
            "expected_move": risk_metrics['expected_move'], 
            "em_lower": risk_metrics['em_lower'], "em_upper": risk_metrics['em_upper'],
            "strategy": strategy, "target_date": target_expiry_date, "dte": days_to_expiry, 
            "strike": best_contract['strike'], 
            "bid": risk_metrics['bid'], "ask": risk_metrics['ask'], 
            "spread": risk_metrics['spread'], "spread_ratio": risk_metrics['spread_ratio'],
            "delta": best_contract['bs_delta'], "iv": best_contract['impliedVolatility'],
            "aroc": aroc,
            "alloc_pct": alloc_pct,
            "margin_per_contract": margin_per_contract,
            "vrp": risk_metrics['vrp']
        }

    except Exception as e:
        print(f"分析 {symbol} 錯誤: {e}")
        return None
