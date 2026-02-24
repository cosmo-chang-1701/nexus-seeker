import math
import pandas as pd
import pandas_ta as ta
import numpy as np
import yfinance as yf
from datetime import datetime
from config import TARGET_DELTAS
from .greeks import calculate_contract_delta
from .data import get_next_earnings_date

import logging

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
        if pd.isna(hv_current):
            return None
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

    # 1. 極端超賣/超買的反轉收租策略 (維持不變)
    if rsi < 35 and hv_rank >= 30:
        return "STO_PUT", "put", deltas.get("STO_PUT", -0.20), 30, 45
    elif rsi > 65 and hv_rank >= 30:
        return "STO_CALL", "call", deltas.get("STO_CALL", 0.20), 30, 45
    # 2. 趨勢跟隨策略 (動態切換買賣方)
    elif price > sma20 and 50 <= rsi <= 65 and macd_hist > 0:
        # 多頭趨勢：若波動率低，買 Call 以小博大；若波動率高，賣 Put 收租
        if hv_rank < 50:
            return "BTO_CALL", "call", deltas.get("BTO_CALL", 0.50), 30, 60
        else:
            return "STO_PUT", "put", deltas.get("STO_PUT", -0.20), 14, 30
    elif price < sma20 and 35 <= rsi <= 50 and macd_hist < 0:
        # 空頭趨勢：若波動率低 (剛起跌)，買 Put 順勢；若波動率高 (已恐慌)，賣 Call 賺溢價
        if hv_rank < 50:
            return "BTO_PUT", "put", deltas.get("BTO_PUT", -0.50), 30, 60
        else:
            # 轉為賣方，賣出微 OTM 的 Call 來做空
            return "STO_CALL", "call", deltas.get("STO_CALL", 0.20), 14, 30
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
                    c_price = 0
                    if not calls_mmm.empty:
                        atm_call_idx = (calls_mmm['strike'] - price).abs().idxmin()
                        atm_call = calls_mmm.loc[atm_call_idx]
                        c_bid, c_ask, c_last = atm_call['bid'], atm_call['ask'], atm_call['lastPrice']
                        c_price = (c_bid + c_ask)/2 if (c_bid > 0 and c_ask > 0) else c_last

                    # Put Price
                    puts_mmm = chain_mmm.puts
                    p_price = 0
                    if not puts_mmm.empty:
                        atm_put_idx = (puts_mmm['strike'] - price).abs().idxmin()
                        atm_put = puts_mmm.loc[atm_put_idx]
                        p_bid, p_ask, p_last = atm_put['bid'], atm_put['ask'], atm_put['lastPrice']
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
            
            if not front_chain.empty and not back_chain.empty:
                front_iv_idx = (front_chain['strike'] - price).abs().idxmin()
                back_iv_idx = (back_chain['strike'] - price).abs().idxmin()
                front_iv = front_chain.loc[front_iv_idx, 'impliedVolatility']
                back_iv = back_chain.loc[back_iv_idx, 'impliedVolatility']
                
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

    # 強行靜音 yfinance 的 ETF 404 報錯洗版
    logging.getLogger('yfinance').setLevel(logging.CRITICAL)

    try:
        # 抓取年化股息殖利率 (Annual Dividend Yield)
        info = ticker.info
        dividend_yield = info.get('dividendYield', 0.0) if info else 0.0
        if dividend_yield is None:
            dividend_yield = 0.0
    except Exception:
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
            
            call_25_idx = (calls_skew['bs_delta'] - 0.25).abs().idxmin()
            put_25_idx = (puts_skew['bs_delta'] - (-0.25)).abs().idxmin()
            call_25 = calls_skew.loc[[call_25_idx]]
            put_25 = puts_skew.loc[[put_25_idx]]
            
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

def _evaluate_option_liquidity(option_data: dict) -> dict:
    """
    評估期權報價的流動性與買賣價差，回傳適合放入 Discord Embed 的狀態與建議。
    
    預期 option_data 包含:
    - bid (float): 買價
    - ask (float): 賣價
    - oi (int): 未平倉量
    - volume (int): 當日成交量
    - dte (int): 距離到期天數
    - delta (float): Delta 值 (正負皆可)
    """
    bid = option_data.get('bid', 0.0)
    ask = option_data.get('ask', 0.0)
    oi = option_data.get('oi', 0)
    volume = option_data.get('volume', 0)
    dte = option_data.get('dte', 0)
    delta = abs(option_data.get('delta', 0.5)) # 取絕對值方便判斷價內外

    # 基本防呆與計算
    if ask <= 0 or bid < 0 or ask <= bid:
        return {"status": "🔴 異常", "embed_msg": "報價異常 (Ask 需大於 Bid 且大於 0)", "is_pass": False}

    mid_price = (bid + ask) / 2
    abs_spread = ask - bid
    rel_spread = abs_spread / mid_price

    # ==========================================
    # 第一道防線：基礎流動性過濾 (OI & Volume)
    # ==========================================
    if oi < 100 or volume < 10:
        return {
            "status": "🔴 極差", 
            "embed_msg": f"流動性枯竭 (OI: {oi}, Vol: {volume})，滑價風險極高", 
            "is_pass": False
        }

    # ==========================================
    # 第二道防線：動態閾值設定 (Dynamic Thresholds)
    # ==========================================
    # 基準相對價差容忍度設為 10%
    max_rel_spread = 0.10 
    
    # 遠月合約 (DTE > 90) 造市商風險大，放寬 5%
    if dte > 90:
        max_rel_spread += 0.05 
        
    # 深價內 (ITM, Delta > 0.8) 或 深價外 (OTM, Delta < 0.15) 流動性自然較差，放寬 5%
    if delta > 0.80 or delta < 0.15:
        max_rel_spread += 0.05 

    # ==========================================
    # 第三道防線：雙軌價差評估 (絕對 vs 相對)
    # ==========================================
    is_spread_valid = True
    
    if ask < 1.00:
        # 【絕對價差檢驗】期權極度便宜時，相對價差會失真，改看絕對價差是否 <= $0.10
        if abs_spread > 0.10:
            is_spread_valid = False
    else:
        # 【相對價差檢驗】一般期權使用動態相對價差閾值
        if rel_spread > max_rel_spread:
            is_spread_valid = False

    if not is_spread_valid:
        return {
            "status": "🔴 警示", 
            "embed_msg": f"價差過寬 (Spread: {rel_spread:.1%}, 絕對值: ${abs_spread:.2f})", 
            "is_pass": False
        }

    # ==========================================
    # 第四階段：分級與 Discord 呈現建議
    # ==========================================
    # 嚴格依照 Spread 比例劃分：< 5% 優良, 5~10% 尚可, > 10% 警告
    if rel_spread < 0.05:
        return {
            "status": "🟢 優良", 
            "embed_msg": f"流動性極佳 (Spread: {rel_spread:.1%}) | 建議：可嘗試掛 Mid-price 成交", 
            "is_pass": True
        }
    elif rel_spread <= 0.10:
        return {
            "status": "🟡 尚可", 
            "embed_msg": f"流動性普通 (Spread: {rel_spread:.1%}) | 建議：嚴格掛 Mid-price 等待成交", 
            "is_pass": True
        }
    else:
        # 當動態放寬規則（如遠月合約或深價外）讓 >10% 的合約通過時的保底提示
        return {
            "status": "🔴 警告", 
            "embed_msg": f"流動性較差 (Spread: {rel_spread:.1%}) | 滑價風險高，務必堅守限價單", 
            "is_pass": True 
        }


def _validate_risk_and_liquidity(strategy, best_contract, price, hv_current, days_to_expiry, symbol):
    """驗證流動性、VRP 與 預期波動 (整合動態流動性評估)"""
    bid = best_contract['bid']
    ask = best_contract['ask']
    strike = best_contract['strike']
    iv = best_contract['impliedVolatility']
    delta = best_contract['bs_delta']

    # yfinance 的未平倉量欄位為 'openInterest'，並確保處理 NaN
    oi = best_contract.get('openInterest', 0)
    oi = 0 if pd.isna(oi) else int(oi)
    
    volume = best_contract.get('volume', 0)
    volume = 0 if pd.isna(volume) else int(volume)
    
    # 1. 流動性
    option_data_for_liq = {
        'bid': bid, 'ask': ask, 'oi': oi, 'volume': volume, 
        'dte': days_to_expiry, 'delta': delta
    }
    
    liq_eval = _evaluate_option_liquidity(option_data_for_liq)
    
    if not liq_eval['is_pass']:
        print(f"[{symbol}] 剔除: {liq_eval['status']} - {liq_eval['embed_msg']}")
        return None
        
    # 2. VRP (僅賣方)
    vrp = iv - hv_current
    if strategy in ["STO_PUT", "STO_CALL"]:
        if vrp < 0:
            print(f"[{symbol}] 剔除: 賣方策略但 VRP {vrp*100:.2f}% < 0 (IV 被低估，無風險溢酬)")
            return None

    elif strategy in ["BTO_PUT", "BTO_CALL"]:
        # 買方遇到過高溢價 (例如 > 3%)，直接擋下！
        if vrp > 0.03: 
            print(f"[{symbol}] 剔除: 買方策略但 VRP 高達 {vrp*100:.2f}% (保費遭恐慌暴拉，拒絕建倉)")
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

    # 中間價計算
    mid_price = (ask + bid) / 2.0
    spread = ask - bid
    spread_ratio = (spread / mid_price) * 100 if mid_price > 0 else 999.0

    # 計算建議的避險腳位 (Short Leg) 來組成垂直價差
    # 我們利用 1σ 預期波動的邊緣作為建議賣出的履約價
    suggested_hedge_strike = None
    if strategy == "BTO_CALL":
        # 買 Call 的同時，建議賣出預期漲幅上限 (em_upper) 附近的 Call
        suggested_hedge_strike = em_upper
    elif strategy == "BTO_PUT":
        # 買 Put 的同時，建議賣出預期跌幅下限 (em_lower) 附近的 Put
        suggested_hedge_strike = em_lower
            
    return {
        "bid": bid, "ask": ask, "spread": spread, "spread_ratio": spread_ratio,
        "vrp": vrp, "expected_move": expected_move, "em_lower": em_lower, "em_upper": em_upper,
        "mid_price": mid_price,
        "suggested_hedge_strike": suggested_hedge_strike,
        "liq_status": liq_eval['status'],
        "liq_msg": liq_eval['embed_msg']
    }

def _calculate_sizing(strategy, best_contract, days_to_expiry, expected_move=0.0, price=0.0, stock_cost=0.0):
    """計算資金效率與倉位大小"""
    aroc = 0.0
    alloc_pct = 0.0
    margin_per_contract = 0.0
    
    bid = best_contract['bid']
    ask = best_contract['ask']
    strike = best_contract['strike']
    delta = best_contract['bs_delta']
    
    if strategy in ["STO_PUT", "STO_CALL"]:
        if strategy == "STO_PUT":
            # 1. 現金擔保賣權 (Cash-Secured Put)
            margin_required = strike - bid 
        else: # STO_CALL
            if stock_cost > 0.0:
                # 🛡️ 掩護性買權：保證金要求 = 現股成本 (100股成本)
                margin_required = stock_cost
            else:
                # 2. 裸賣買權：Reg T 粗估公式
                # 美股 Reg T 粗估：20% 標的現價 - 價外金額 + 權利金 (最低不低於 10% 現價)
                if price > 0:
                    otm_amount = max(0, strike - price)
                    margin_required = max((0.20 * price) - otm_amount + bid, 0.10 * price + bid)
                else:
                    margin_required = strike # 防呆後備方案

        # 賣方：以保證金為成本基礎
        if margin_required > 0:
            aroc = (bid / margin_required) * (365.0 / max(days_to_expiry, 1)) * 100
            if aroc >= 15.0:
                b = bid / margin_required
                p = 1.0 - abs(delta)
                if b > 0:
                    q = 1.0 - p
                    kelly_f = (p * b - q) / b
                    alloc_pct = min(max(kelly_f * 0.25, 0.0), 0.05)
                    margin_per_contract = margin_required * 100

    elif strategy in ["BTO_CALL", "BTO_PUT"]:
        # 買方：以權利金 (ask) 為最大風險
        premium = ask
        if premium > 0 and expected_move > 0:
            # 預期報酬 = 預期波動 - 已付權利金，年化後得 AROC
            potential_profit = expected_move - premium
            aroc = (potential_profit / premium) * (365.0 / max(days_to_expiry, 1)) * 100 if potential_profit > 0 else 0.0
            
            if aroc >= 30.0:
                # 買方 Kelly：p = |delta| (到價機率), b = 預期報酬/權利金
                p = abs(delta)
                b = potential_profit / premium if potential_profit > 0 else 0.0
                if b > 0:
                    q = 1.0 - p
                    kelly_f = (p * b - q) / b
                    # 買方更保守：quarter-Kelly，上限 3%
                    alloc_pct = min(max(kelly_f * 0.25, 0.0), 0.03)
            margin_per_contract = premium * 100
                    
    return aroc, alloc_pct, margin_per_contract

def analyze_symbol(symbol, stock_cost=0.0):
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
        aroc, alloc_pct, margin_per_contract = _calculate_sizing(
            strategy,
            best_contract,
            days_to_expiry,
            expected_move=risk_metrics['expected_move'],
            price=price,
            stock_cost=stock_cost
        )
        if strategy in ["STO_PUT", "STO_CALL"] and aroc < 15.0:
            return None
        if strategy in ["BTO_CALL", "BTO_PUT"] and aroc < 30.0:
            return None

        # 8. 組合結果
        return {
            "symbol": symbol, "price": price,
            "stock_cost": stock_cost,
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
            "vrp": risk_metrics['vrp'],
            "mid_price": risk_metrics['mid_price'],
            "suggested_hedge_strike": risk_metrics['suggested_hedge_strike'],
            "liq_status": risk_metrics['liq_status'],
            "liq_msg": risk_metrics['liq_msg']
        }

    except Exception as e:
        print(f"分析 {symbol} 錯誤: {e}")
        return None
