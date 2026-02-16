import pandas as pd
import pandas_ta as ta
import numpy as np
import yfinance as yf
from datetime import datetime
from py_vollib.black_scholes.greeks.analytical import delta
from config import RISK_FREE_RATE, TARGET_DELTAS

def calculate_contract_delta(row, current_price, t_years, flag):
    """
    計算單一選擇權合約的理論 Delta 值。

    Args:
        row (pd.Series): 包含 impliedVolatility 與 strike 的資料列。
        current_price (float): 標的資產當前價格。
        t_years (float): 距離到期日的年化時間。
        flag (str): 選擇權類型 ('c' for Call, 'p' for Put)。

    Returns:
        float: 計算出的 Delta 值，若失敗或無效則回傳 0.0。
    """
    iv = row['impliedVolatility']
    if pd.isna(iv) or iv <= 0.01:
        return 0.0
    try:
        return delta(flag, current_price, row['strike'], t_years, RISK_FREE_RATE, iv)
    except Exception:
        return 0.0

def get_next_earnings_date(ticker):
    """
    取得下一次財報發布日期。

    Args:
        ticker (yf.Ticker): yfinance Ticker 物件。

    Returns:
        datetime.date or None: 下一次財報日期，若無資料則回傳 None。
    """
    try:
        # 避免重複建立 ticker 物件，直接使用傳入的實例
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
    """
    掃描技術指標、波動率位階、期限結構與造市商預期波動，並過濾最佳合約。

    Args:
        symbol (str): 股票代碼。

    Returns:
        dict or None: 分析結果字典，若無符合策略則回傳 None。
    """
    try:
        ticker = yf.Ticker(symbol)
        df = ticker.history(period="1y")
        if df.empty or len(df) < 50:
            return None

        # --- 1. 歷史波動率位階 (HV Rank) ---
        df['Log_Ret'] = np.log(df['Close'] / df['Close'].shift(1))
        df['HV_20'] = df['Log_Ret'].rolling(window=20).std() * np.sqrt(252)
        hv_min = df['HV_20'].min()
        hv_max = df['HV_20'].max()
        hv_current = df['HV_20'].iloc[-1]
        hv_rank = ((hv_current - hv_min) / (hv_max - hv_min)) * 100 if hv_max > hv_min else 0.0

        # --- 2. 價格技術指標 ---
        df.ta.rsi(length=14, append=True)
        df.ta.sma(length=20, append=True)
        df.ta.macd(fast=12, slow=26, signal=9, append=True)
        
        latest = df.iloc[-1]
        price = latest['Close']
        rsi = latest['RSI_14']
        sma20 = latest['SMA_20']
        macd_hist = latest['MACDh_12_26_9']

        strategy, opt_type, target_delta, min_dte, max_dte = None, None, 0, 0, 0
        
        # --- 策略決策樹 ---
        if rsi < 35 and hv_rank >= 30:
            strategy = "STO_PUT"
            opt_type = "put"
            target_delta = TARGET_DELTAS["STO_PUT"]
            min_dte, max_dte = 30, 45
        elif rsi > 65 and hv_rank >= 30:
            strategy = "STO_CALL"
            opt_type = "call"
            target_delta = TARGET_DELTAS["STO_CALL"]
            min_dte, max_dte = 30, 45
        elif price > sma20 and 50 <= rsi <= 65 and macd_hist > 0:
            strategy = "BTO_CALL"
            opt_type = "call"
            target_delta = TARGET_DELTAS["BTO_CALL"]
            min_dte, max_dte = 14, 30
        elif price < sma20 and 35 <= rsi <= 50 and macd_hist < 0:
            strategy = "BTO_PUT"
            opt_type = "put"
            target_delta = TARGET_DELTAS["BTO_PUT"]
            min_dte, max_dte = 14, 30
        else:
            return None 

        expirations = ticker.options
        if not expirations:
            return None
        today = datetime.now().date()

        # ==========================================
        # 🔥 量化運算 2.5: 財報倒數與造市商預期波動 (MMM)
        # ==========================================
        # 直接傳入 ticker 物件以節省 API 呼叫
        earnings_date = get_next_earnings_date(ticker)
        days_to_earnings = -1
        mmm_pct, safe_lower, safe_upper = 0.0, 0.0, 0.0

        if earnings_date:
            if isinstance(earnings_date, datetime):
                earnings_date = earnings_date.date()
            days_to_earnings = (earnings_date - today).days
            
            # 若財報在 14 天內，系統啟動 MMM 精算機制
            if 0 <= days_to_earnings <= 14:
                # 尋找涵蓋財報日的「最近到期日」來計算 Straddle
                target_exp_for_mmm = None
                for exp in expirations:
                    if datetime.strptime(exp, '%Y-%m-%d').date() >= earnings_date:
                        target_exp_for_mmm = exp
                        break
                
                if target_exp_for_mmm:
                    try:
                        chain_mmm = ticker.option_chain(target_exp_for_mmm)
                        
                        # 抓取 ATM Call
                        calls_mmm = chain_mmm.calls
                        atm_call_idx = (calls_mmm['strike'] - price).abs().argsort()[:1]
                        if not atm_call_idx.empty:
                            atm_call = calls_mmm.iloc[atm_call_idx]
                            c_bid = atm_call['bid'].values[0]
                            c_ask = atm_call['ask'].values[0]
                            c_last = atm_call['lastPrice'].values[0]
                            c_price = (c_bid + c_ask)/2 if (c_bid > 0 and c_ask > 0) else c_last
                        else:
                            c_price = 0

                        # 抓取 ATM Put
                        puts_mmm = chain_mmm.puts
                        atm_put_idx = (puts_mmm['strike'] - price).abs().argsort()[:1]
                        if not atm_put_idx.empty:
                            atm_put = puts_mmm.iloc[atm_put_idx]
                            p_bid = atm_put['bid'].values[0]
                            p_ask = atm_put['ask'].values[0]
                            p_last = atm_put['lastPrice'].values[0]
                            p_price = (p_bid + p_ask)/2 if (p_bid > 0 and p_ask > 0) else p_last
                        else:
                            p_price = 0
                        
                        # MMM 數學公式: (ATM Straddle 價格 / 現價) * 100
                        if price > 0:
                            mmm_pct = ((c_price + p_price) / price) * 100
                            safe_lower = price * (1 - mmm_pct / 100)
                            safe_upper = price * (1 + mmm_pct / 100)
                    except Exception as e:
                        print(f"[{symbol}] MMM 運算失敗: {e}")

        # --- 3. 波動率期限結構 (Term Structure) ---
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
                
                # 簡單取最接近價平的 IV
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

        # --- 4. Greeks 精算與尋標 ---
        target_expiry_date = None
        for exp in expirations:
            days_to_expiry = (datetime.strptime(exp, '%Y-%m-%d').date() - today).days
            if min_dte <= days_to_expiry <= max_dte:
                target_expiry_date = exp
                break
        
        if not target_expiry_date:
            return None

        opt_chain = ticker.option_chain(target_expiry_date)
        chain_data = opt_chain.calls if opt_type == "call" else opt_chain.puts
        chain_data = chain_data[chain_data['volume'] > 0].copy()
        
        if chain_data.empty:
            return None

        # 計算 Greeks
        t_years = max(days_to_expiry, 1) / 365.0
        chain_data['bs_delta'] = chain_data.apply(
            lambda row: calculate_contract_delta(row, price, t_years, 'c' if opt_type=="call" else 'p'), 
            axis=1
        )
        chain_data = chain_data[chain_data['bs_delta'] != 0.0].copy()
        
        if chain_data.empty:
            return None

        # 選出 Delta 最接近目標值的合約
        best_contract = chain_data.loc[(chain_data['bs_delta'] - target_delta).abs().idxmin()]

        # --- 4.5: 垂直波動率偏態 (Vertical Skew) ---
        vertical_skew = 1.0
        skew_state = "⚖️ 中性 (Neutral)"
        
        try:
            # 取得該到期日的完整 Call 與 Put 報價表
            calls_skew = opt_chain.calls[opt_chain.calls['volume'] > 0].copy()
            puts_skew = opt_chain.puts[opt_chain.puts['volume'] > 0].copy()
            
            if not calls_skew.empty and not puts_skew.empty:
                # 分別計算整張報價表的 Delta
                calls_skew['bs_delta'] = calls_skew.apply(lambda row: calculate_contract_delta(row, price, t_years, 'c'), axis=1)
                puts_skew['bs_delta'] = puts_skew.apply(lambda row: calculate_contract_delta(row, price, t_years, 'p'), axis=1)
                
                # 尋找 25 Delta 的 OTM Call 與 -0.25 Delta 的 OTM Put
                call_25 = calls_skew.iloc[(calls_skew['bs_delta'] - 0.25).abs().argsort()[:1]]
                put_25 = puts_skew.iloc[(puts_skew['bs_delta'] - (-0.25)).abs().argsort()[:1]]
                
                if not call_25.empty and not put_25.empty:
                    iv_call_25 = call_25['impliedVolatility'].values[0]
                    iv_put_25 = put_25['impliedVolatility'].values[0]
                    
                    if iv_call_25 > 0.01:
                        # 偏態比率 = 25 Delta Put IV / 25 Delta Call IV
                        vertical_skew = iv_put_25 / iv_call_25
                        
                    # 偏態極端值判定與防禦機制
                    if vertical_skew >= 1.30:
                        skew_state = "⚠️ 嚴重左偏 (高尾部風險)"
                        # 硬性濾網：當偏態過於極端，否決 STO_PUT 訊號，規避單邊崩盤風險
                        if strategy == "STO_PUT" and vertical_skew >= 1.50:
                            print(f"[{symbol}] 剔除: 垂直偏態比率 {vertical_skew:.2f} 過高，拒絕承接下行風險")
                            return None
                    elif vertical_skew <= 0.90:
                        skew_state = "🚀 右偏 (看漲狂熱)"
        except Exception as e:
            print(f"[{symbol}] 垂直偏態運算錯誤: {e}")

        # --- 5. 買賣價差與流動性濾網 (Slippage Filter) ---
        bid_price = best_contract['bid']
        ask_price = best_contract['ask']

        # 基礎防呆：若報價為 0 或遺失，直接視為無效合約
        if bid_price <= 0 or ask_price <= 0:
            print(f"[{symbol}] 剔除: 報價異常 (Bid或Ask <= 0)")
            return None

        spread = ask_price - bid_price
        mid_price = (ask_price + bid_price) / 2.0
        
        # 計算價差佔比 (%)
        spread_ratio = (spread / mid_price) * 100 if mid_price > 0 else 999.0

        # 硬性濾網：若絕對價差 > $0.20 且 價差比例 > 10%，判定為流動性陷阱
        # (代表你要越過極大的鴻溝才能成交，期望值被嚴重侵蝕)
        if spread > 0.20 and spread_ratio > 10.0:
            print(f"[{symbol}] 剔除: 流動性極差 (價差 ${spread:.2f}, 佔比 {spread_ratio:.1f}%)")
            return None

        # --- 量化運算 5.5: 波動率風險溢酬 (VRP) 濾網 ---
        # hv_current 是我們在最前面算出的 20日歷史波動率 (Annualized)
        iv_current = best_contract['impliedVolatility']
        
        # VRP = 當前合約隱含波動率 - 標的資產 20 日實現波動率
        vrp = iv_current - hv_current
        
        if strategy in ["STO_PUT", "STO_CALL"]:
            # 賣方嚴格濾網：拒絕在 IV 被低估 (VRP < 0) 時承擔無限風險
            if vrp < 0:
                print(f"[{symbol}] 剔除: VRP {vrp*100:.2f}% < 0 (IV 被低估，無風險溢酬)")
                return None

        # --- 6. AROC 資金效率 ---
        bid_price = best_contract['bid']
        strike_price = best_contract['strike']
        aroc = 0.0
        
        if "STO" in strategy:
            # 賣方策略檢查
            if bid_price <= 0 or (strike_price - bid_price) <= 0:
                return None
            # Annualized Return on Capital
            aroc = (bid_price / (strike_price - bid_price)) * (365.0 / max(days_to_expiry, 1)) * 100
            if aroc < 15.0:
                return None

        # --- 7. 小數凱利準則 ---
        alloc_pct = 0.0
        margin_per_contract = 0.0
        
        if strategy in ["STO_PUT", "STO_CALL"] and aroc >= 15.0:
            # 保證金近似 = 履約價 - 權利金收入
            margin_required = strike_price - bid_price
            if margin_required <= 0:
                return None
            # 賠率 b = 預期獲利 / 最大承擔風險
            b = bid_price / margin_required
            # 勝率 p 近似於 (1 - Delta絕對值)
            p = 1.0 - abs(best_contract['bs_delta'])
            
            if b > 0:
                # 傳統凱利公式
                kelly_f = (p * (b + 1) - 1) / b
                
                # 採用 1/4 凱利 (Quarter Kelly)，並設定單一標的硬上限 5%
                alloc_pct = min(max(kelly_f * 0.25, 0.0), 0.05)
                
                # 計算單口保證金 (合約乘數 100)
                margin_per_contract = margin_required * 100

        return {
            "symbol": symbol, "price": price, "rsi": rsi, "sma20": sma20,
            "hv_rank": hv_rank, "ts_ratio": ts_ratio, "ts_state": ts_state,
            "v_skew": vertical_skew, "v_skew_state": skew_state,
            "earnings_days": days_to_earnings, "mmm_pct": mmm_pct,
            "safe_lower": safe_lower, "safe_upper": safe_upper,
            "strategy": strategy, "target_date": target_expiry_date, "dte": days_to_expiry, 
            "strike": strike_price, "bid": bid_price, "ask": best_contract['ask'], 
            "spread": spread, "spread_ratio": spread_ratio,
            "delta": best_contract['bs_delta'], "iv": best_contract['impliedVolatility'],
            "aroc": aroc,
            "alloc_pct": alloc_pct,                     # 輸出凱利建議資金佔比
            "margin_per_contract": margin_per_contract, # 輸出單口保證金
            "vrp": vrp
        }
    except Exception as e:
        print(f"分析 {symbol} 錯誤: {e}")
        return None

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
                        current_delta = delta(flag, current_stock_price, strike, t_years, RISK_FREE_RATE, iv)
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
    # 🔥 宏觀風險診斷報告 (附加於列表最下方)
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

    return report_lines