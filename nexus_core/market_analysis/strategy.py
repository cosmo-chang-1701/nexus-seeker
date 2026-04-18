import math
from typing import Optional, Dict, Any, List
import pandas as pd
import pandas_ta as ta
import numpy as np
import yfinance as yf  # 僅保留用於 option_chain() / options
from services import market_data_service
from datetime import datetime
from config import TARGET_DELTAS, get_vix_tier
from .greeks import calculate_contract_delta, calculate_greeks
from .data import get_next_earnings_date

from .risk_engine import calculate_beta

import logging
import asyncio

logger = logging.getLogger(__name__)

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
            'price': latest.get('Close'),
            'rsi': latest.get('RSI_14'),
            'sma20': latest.get('SMA_20'),
            'macd_hist': latest.get('MACDh_12_26_9'),
            'hv_current': hv_current,
            'hv_rank': hv_rank
        }
    except Exception as e:
        logger.error(f"指標計算錯誤: {e}")
        return None

def _determine_strategy_signal(indicators):
    """根據技術指標決定策略"""
    price = indicators.get('price', 0.0)
    rsi = indicators.get('rsi', 50.0)
    hv_rank = indicators.get('hv_rank', 0.0)
    sma20 = indicators.get('sma20', 0.0)
    macd_hist = indicators.get('macd_hist', 0.0)

    deltas = TARGET_DELTAS if TARGET_DELTAS else {}

    if rsi < 35 and hv_rank >= 30:
        return "STO_PUT", "put", deltas.get("STO_PUT", -0.20), 30, 45
    elif rsi > 65 and hv_rank >= 30:
        return "STO_CALL", "call", deltas.get("STO_CALL", 0.20), 30, 45
    elif price > sma20 and 50 <= rsi <= 65 and macd_hist > 0:
        if hv_rank < 50:
            return "BTO_CALL", "call", deltas.get("BTO_CALL", 0.50), 30, 60
        else:
            return "STO_PUT", "put", deltas.get("STO_PUT", -0.20), 14, 30
    elif price < sma20 and 35 <= rsi <= 50 and macd_hist < 0:
        if hv_rank < 50:
            return "BTO_PUT", "put", deltas.get("BTO_PUT", -0.50), 30, 60
        else:
            return "STO_CALL", "call", deltas.get("STO_CALL", 0.20), 14, 30
    else:
        return None, None, 0, 0, 0

async def _calculate_mmm(ticker, price, today, symbol, is_etf):
    """計算財報日 MMM (Market Maker Move)"""
    earnings_date = None if is_etf else await get_next_earnings_date(symbol)
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
                    calls_mmm = chain_mmm.calls
                    c_price = 0
                    if not calls_mmm.empty:
                        atm_call_idx = (calls_mmm['strike'] - price).abs().idxmin()
                        atm_call = calls_mmm.loc[atm_call_idx]
                        c_bid, c_ask, c_last = atm_call.get('bid', 0.0), atm_call.get('ask', 0.0), atm_call.get('lastPrice', 0.0)
                        c_price = (c_bid + c_ask)/2 if (c_bid > 0 and c_ask > 0) else c_last

                    puts_mmm = chain_mmm.puts
                    p_price = 0
                    if not puts_mmm.empty:
                        atm_put_idx = (puts_mmm['strike'] - price).abs().idxmin()
                        atm_put = puts_mmm.loc[atm_put_idx]
                        p_bid, p_ask, p_last = atm_put.get('bid', 0.0), atm_put.get('ask', 0.0), atm_put.get('lastPrice', 0.0)
                        p_price = (p_bid + p_ask)/2 if (p_bid > 0 and p_ask > 0) else p_last
                    
                    if price > 0:
                        mmm_pct = ((c_price + p_price) / price) * 100
                        safe_lower = price * (1 - mmm_pct / 100)
                        safe_upper = price * (1 + mmm_pct / 100)
                except Exception as e:
                    logger.error(f"[{symbol}] MMM 運算失敗: {e}")

    return mmm_pct, safe_lower, safe_upper, days_to_earnings

def _calculate_term_structure(ticker, expirations, price, today):
    """計算波動率期限結構"""
    front_date, back_date = None, None
    front_diff, back_diff = 9999, 9999
    
    for exp in expirations:
        days_to_expiry = (datetime.strptime(exp, '%Y-%m-%d').date() - today).days
        if abs(days_to_expiry - 30) < front_diff:
            front_diff, front_date = abs(days_to_expiry - 30), exp
        if abs(days_to_expiry - 60) < back_diff:
            back_diff, back_date = abs(days_to_expiry - 60), exp
            
    ts_ratio, ts_state = 1.0, "平滑 (Flat)"
    if front_date and back_date and front_date != back_date:
        try:
            front_chain = ticker.option_chain(front_date).puts
            back_chain = ticker.option_chain(back_date).puts
            
            if not front_chain.empty and not back_chain.empty:
                front_iv_idx = (front_chain['strike'] - price).abs().idxmin()
                back_iv_idx = (back_chain['strike'] - price).abs().idxmin()
                front_iv = front_chain.loc[front_iv_idx].get('impliedVolatility', 0.0)
                back_iv = back_chain.loc[back_iv_idx].get('impliedVolatility', 0.0)
                
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

def _get_best_contract_data(ticker, target_expiry_date, opt_type, target_delta, price, days_to_expiry, dividend_yield=0.0):
    """取得最佳合約與 Greeks"""
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

def _calculate_vertical_skew(opt_chain, price, days_to_expiry, strategy, symbol, dividend_yield=0.0):
    """計算垂直波動率偏態"""
    vertical_skew = 1.0
    skew_state = "⚖️ 中性 (Neutral)"
    t_years = max(days_to_expiry, 1) / 365.0
    is_high_tail_risk = False
    
    try:
        calls_skew = opt_chain.calls[opt_chain.calls['volume'] > 0].copy()
        puts_skew = opt_chain.puts[opt_chain.puts['volume'] > 0].copy()
        
        if not calls_skew.empty and not puts_skew.empty:
            calls_skew['bs_delta'] = calls_skew.apply(lambda row: calculate_contract_delta(row, price, t_years, 'c', q=dividend_yield), axis=1)
            puts_skew['bs_delta'] = puts_skew.apply(lambda row: calculate_contract_delta(row, price, t_years, 'p', q=dividend_yield), axis=1)
            
            call_25_idx = (calls_skew['bs_delta'] - 0.25).abs().idxmin()
            put_25_idx = (puts_skew['bs_delta'] - (-0.25)).abs().idxmin()
            call_25 = calls_skew.loc[[call_25_idx]]
            put_25 = puts_skew.loc[[put_25_idx]]
            
            if not call_25.empty and not put_25.empty:
                iv_call_25 = call_25.iloc[0].get('impliedVolatility', 0.0)
                iv_put_25 = put_25.iloc[0].get('impliedVolatility', 0.0)
                
                if iv_call_25 > 0.01:
                    vertical_skew = iv_put_25 / iv_call_25
                    
                if vertical_skew >= 1.30:
                    skew_state = "⚠️ 嚴重左偏 (高尾部風險)"
                    if vertical_skew >= 1.50:
                        skew_state = "🚨 極端左偏 (觸發尾部風險降規)"
                        is_high_tail_risk = True
                elif vertical_skew <= 0.90:
                    skew_state = "🚀 右偏 (看漲狂熱)"
    except Exception as e:
        logger.error(f"[{symbol}] 垂直偏態運算錯誤: {e}")
        
    return vertical_skew, skew_state, is_high_tail_risk

def _evaluate_option_liquidity(option_data: dict) -> dict:
    """評估期權報價的流動性與買賣價差。"""
    bid = option_data.get('bid', 0.0)
    ask = option_data.get('ask', 0.0)
    oi = option_data.get('oi', 0)
    volume = option_data.get('volume', 0)
    dte = option_data.get('dte', 0)
    delta = abs(option_data.get('delta', 0.5))

    if ask <= 0 or bid < 0 or ask <= bid:
        return {"status": "🔴 異常", "embed_msg": "報價異常 (Ask 需大於 Bid 且大於 0)", "is_pass": False}

    mid_price = (bid + ask) / 2
    abs_spread = ask - bid
    rel_spread = abs_spread / mid_price

    if oi < 100 or volume < 10:
        return {
            "status": "🔴 極差", 
            "embed_msg": f"流動性枯竭 (OI: {oi}, Vol: {volume})，滑價風險極高", 
            "is_pass": False
        }

    max_rel_spread = 0.10 
    if dte > 90: max_rel_spread += 0.05 
    if delta > 0.80 or delta < 0.15: max_rel_spread += 0.05 

    is_spread_valid = True
    if ask < 1.00:
        if abs_spread > 0.10: is_spread_valid = False
    else:
        if rel_spread > max_rel_spread: is_spread_valid = False

    if not is_spread_valid:
        return {
            "status": "🔴 警示", 
            "embed_msg": f"價差過寬 (Spread: {rel_spread:.1%}, 絕對值: ${abs_spread:.2f})", 
            "is_pass": False
        }

    if rel_spread < 0.05:
        return {"status": "🟢 優良", "embed_msg": f"流動性極佳 (Spread: {rel_spread:.1%}) | 建議：可嘗試掛 Mid-price 成交", "is_pass": True}
    elif rel_spread <= 0.10:
        return {"status": "🟡 尚可", "embed_msg": f"流動性普通 (Spread: {rel_spread:.1%}) | 建議：嚴格掛 Mid-price 等待成交", "is_pass": True}
    else:
        return {"status": "🔴 警告", "embed_msg": f"流動性較差 (Spread: {rel_spread:.1%}) | 滑價風險高，務必堅守限價單", "is_pass": True}

def _validate_risk_and_liquidity(strategy, best_contract, price, hv_current, days_to_expiry, symbol):
    """驗證流動性、VRP 與 預期波動。"""
    bid = best_contract.get('bid', 0.0)
    ask = best_contract.get('ask', 0.0)
    strike = best_contract.get('strike', 0.0)
    iv = best_contract.get('impliedVolatility', 0.0)
    delta = best_contract.get('bs_delta', 0.0)

    oi = best_contract.get('openInterest', 0)
    oi = 0 if pd.isna(oi) else int(oi)
    volume = best_contract.get('volume', 0)
    volume = 0 if pd.isna(volume) else int(volume)
    
    option_data_for_liq = {'bid': bid, 'ask': ask, 'oi': oi, 'volume': volume, 'dte': days_to_expiry, 'delta': delta}
    liq_eval = _evaluate_option_liquidity(option_data_for_liq)
    
    if not liq_eval['is_pass']:
        logger.info(f"[{symbol}] 剔除: {liq_eval['status']} - {liq_eval['embed_msg']}")
        return None
        
    vrp = iv - hv_current
    if strategy in ["STO_PUT", "STO_CALL"]:
        if vrp < 0:
            logger.info(f"[{symbol}] 剔除: 賣方策略但 VRP {vrp*100:.2f}% < 0")
            return None
    elif strategy in ["BTO_PUT", "BTO_CALL"]:
        if vrp > 0.03: 
            logger.info(f"[{symbol}] 剔除: 買方策略但 VRP 高達 {vrp*100:.2f}%")
            return None

    expected_move = price * iv * math.sqrt(max(days_to_expiry, 1) / 365.0)
    em_lower = price - expected_move
    em_upper = price + expected_move
    
    if strategy == "STO_PUT":
        breakeven = strike - bid
        if breakeven > em_lower:
            logger.info(f"[{symbol}] 剔除: 損益兩平點 ${breakeven:.2f} 落入 1σ 預期跌幅內")
            return None
    elif strategy == "STO_CALL":
        breakeven = strike + bid
        if breakeven < em_upper:
            logger.info(f"[{symbol}] 剔除: 損益兩平點 ${breakeven:.2f} 落入 1σ 預期漲幅內")
            return None

    mid_price = (ask + bid) / 2.0
    spread = ask - bid
    spread_ratio = (spread / mid_price) * 100 if mid_price > 0 else 999.0

    suggested_hedge_strike = em_upper if strategy == "BTO_CALL" else (em_lower if strategy == "BTO_PUT" else None)
            
    return {
        "bid": bid, "ask": ask, "spread": spread, "spread_ratio": spread_ratio,
        "vrp": vrp, "expected_move": expected_move, "em_lower": em_lower, "em_upper": em_upper,
        "mid_price": mid_price, "suggested_hedge_strike": suggested_hedge_strike,
        "liq_status": liq_eval['status'], "liq_msg": liq_eval['embed_msg']
    }

def apply_vix_ladder(vix_spot: float) -> dict:
    """根據 VIX 即時水位回傳對應的戰情階梯配置。

    回傳值為 tier dict，包含：
    - allow_signal: 是否允許 STO 訊號
    - sto_delta_cap: STO Delta 上限 (負數)
    - sizing_multiplier: 倉位大小乘數
    - kelly_fraction_override: 可選的 Kelly 分數覆寫
    - vtr_entry_allowed: 是否允許 VTR 自動建倉
    """
    return get_vix_tier(vix_spot)


def _calculate_sizing(strategy, best_contract, days_to_expiry, expected_move=0.0, price=0.0, stock_cost=0.0, kelly_fraction=0.5, kelly_fraction_override: Optional[float] = None):
    """計算資金效率與倉位大小
    
    Args:
        kelly_fraction_override: 若非 None，則覆寫 kelly_fraction（用於 VIX All-in 階梯）。
    """
    effective_kelly = kelly_fraction_override if kelly_fraction_override is not None else kelly_fraction
    aroc, alloc_pct, margin_per_contract = 0.0, 0.0, 0.0
    
    bid = best_contract.get('bid', 0.0)
    ask = best_contract.get('ask', 0.0)
    strike = best_contract.get('strike', 0.0)
    delta = best_contract.get('bs_delta', 0.0)
    
    if strategy in ["STO_PUT", "STO_CALL"]:
        margin_required = (strike - bid) if strategy == "STO_PUT" else (stock_cost if stock_cost > 0.0 else max((0.20 * price) - max(0, strike - price) + bid, 0.10 * price + bid) if price > 0 else strike)
        if margin_required > 0:
            aroc = (bid / margin_required) * (365.0 / max(days_to_expiry, 1)) * 100
            if aroc >= 15.0:
                p = 1.0 - abs(delta)
                b = bid / margin_required
                if b > 0:
                    kelly_f = (p * b - (1.0 - p)) / b
                    alloc_pct = min(max(kelly_f * effective_kelly, 0.0), 0.05)
                    margin_per_contract = margin_required * 100
    elif strategy in ["BTO_CALL", "BTO_PUT"]:
        premium = ask
        if premium > 0 and expected_move > 0:
            potential_profit = expected_move - premium
            aroc = (potential_profit / premium) * (365.0 / max(days_to_expiry, 1)) * 100 if potential_profit > 0 else 0.0
            if aroc >= 30.0:
                p = abs(delta)
                b = potential_profit / premium
                if b > 0:
                    kelly_f = (p * b - (1.0 - p)) / b
                    alloc_pct = min(max(kelly_f * effective_kelly, 0.0), 0.03)
            margin_per_contract = premium * 100
                    
    return aroc, alloc_pct, margin_per_contract

async def evaluate_ema_trend(symbol: str, current_price: float) -> dict:
    """評估 EMA 8/21 趨勢狀態。"""
    ema8 = await market_data_service.get_ema(symbol, 8)
    ema21 = await market_data_service.get_ema(symbol, 21)
    
    if not ema8 or not ema21:
        return {"trend": "UNKNOWN", "score": 0, "ema_8": 0.0, "ema_21": 0.0, "distance_from_21": 0.0}

    distance_pct = (current_price - ema21) / ema21
    if current_price > ema8 > ema21: state = "BULLISH_STRONG"
    elif ema8 > ema21 and current_price <= ema21: state = "BULLISH_CORRECTION"
    elif current_price < ema8 < ema21: state = "BEARISH_STRONG"
    else: state = "NEUTRAL"

    return {"trend": state, "ema_8": ema8, "ema_21": ema21, "distance_from_21": round(distance_pct * 100, 2)}

def detect_ema_signals(df: pd.DataFrame, window: int = 21, threshold: float = 0.005) -> Optional[Dict[str, Any]]:
    """偵測價格對 EMA 的穿透與支撐/壓力測試。"""
    if df.empty or len(df) < window + 2: return None
    ema_series = df['Close'].ewm(span=window, adjust=False).mean()
    p_curr, p_prev = df['Close'].iloc[-1], df['Close'].iloc[-2]
    ema_curr, ema_prev = ema_series.iloc[-1], ema_series.iloc[-2]

    signal_type, direction = None, None
    if p_prev < ema_prev and p_curr >= ema_curr: signal_type, direction = "CROSSOVER", "BULLISH"
    elif p_prev > ema_prev and p_curr <= ema_curr: signal_type, direction = "CROSSOVER", "BEARISH"
    if not signal_type:
        dist_pct = abs(p_curr - ema_curr) / ema_curr
        if dist_pct <= threshold: signal_type, direction = "TEST", ("SUPPORT" if p_curr > ema_curr else "RESISTANCE")

    if signal_type:
        return {"window": window, "type": signal_type, "direction": direction, "ema_val": round(ema_curr, 2), "distance_pct": round((p_curr - ema_curr) / ema_curr * 100, 2)}
    return None

async def analyze_symbol(symbol, stock_cost=0.0, df_spy=None, spy_price=None, vix_spot: Optional[float] = None):
    """掃描技術指標、波動率、偏態、Greeks 等進行核心分析。
    
    Args:
        vix_spot: VIX 即時價格。用於 VIX 戰情階梯判定（Delta 上限、倉位縮放、訊號閘門）。
    """
    try:
        ticker = yf.Ticker(symbol)
        quote = await market_data_service.get_quote(symbol)
        price = quote.get('c', 0.0) if quote else None
        is_etf = await market_data_service.is_etf(symbol)

        df = await market_data_service.get_history_df(symbol, "1y")
        if df.empty: return None
        if price is None or price <= 0: price = df['Close'].iloc[-1]

        if is_etf: dividend_yield = 0.015
        else: dividend_yield = await market_data_service.get_dividend_yield(symbol)

        if df_spy is None: df_spy = await market_data_service.get_history_df("SPY", "1y")
        
        if df_spy.empty:
            logger.warning(f"無法取得 SPY 基準資料，{symbol} 改用 beta=1.0 fallback")
            spy_price_val = spy_price if spy_price is not None and spy_price > 0 else price
            beta = 1.0
        else:
            spy_price_val = spy_price if spy_price is not None else df_spy['Close'].iloc[-1]
            beta = calculate_beta(df, df_spy) if symbol != "SPY" else 1.0

        indicators = _calculate_technical_indicators(df)
        if indicators is None: return None
        price = indicators['price'] 

        strategy, opt_type, target_delta, min_dte, max_dte = _determine_strategy_signal(indicators)
        if not strategy: return None

        # ---------- VIX 戰情階梯閘門 (VIX Battle Ladder Gate) ----------
        vix_tier = apply_vix_ladder(vix_spot)
        vix_sizing_multiplier = vix_tier.get('sizing_multiplier', 1.0)
        vix_kelly_override = vix_tier.get('kelly_fraction_override')

        if strategy in ["STO_PUT", "STO_CALL"]:
            if not vix_tier.get('allow_signal', True):
                logger.info(f"[{symbol}] 剔除: VIX {vix_spot:.1f} 處於 '{vix_tier['name']}' 階梯，硬拒所有 STO 訊號")
                return None

            # Delta 上限鉗制：sto_delta_cap 為負數，max() 取較小絕對值（更保守）
            sto_cap = vix_tier.get('sto_delta_cap', -0.20)
            if sto_cap != 0.0 and strategy == "STO_PUT" and target_delta < sto_cap:
                logger.info(f"[{symbol}] VIX 階梯 Delta 鉗制: {target_delta:.2f} -> {sto_cap:.2f}")
                target_delta = sto_cap
            elif sto_cap != 0.0 and strategy == "STO_CALL" and target_delta > abs(sto_cap):
                logger.info(f"[{symbol}] VIX 階梯 Delta 鉗制: {target_delta:.2f} -> {abs(sto_cap):.2f}")
                target_delta = abs(sto_cap)
        # ----------------------------------------------------------------

        expirations = await asyncio.to_thread(lambda: ticker.options)
        if not expirations: return None
        today = datetime.now().date()
        
        target_expiry_date, days_to_expiry = _find_target_expiry(expirations, today, min_dte, max_dte)
        if not target_expiry_date: return None

        best_contract, opt_chain = await asyncio.to_thread(_get_best_contract_data, ticker, target_expiry_date, opt_type, target_delta, price, days_to_expiry, dividend_yield)
        if best_contract is None: return None

        if days_to_expiry <= 90:
            ema_eval = await evaluate_ema_trend(symbol, price)
            if ema_eval.get("trend") == "BEARISH_STRONG" and strategy in ["BTO_CALL", "STO_PUT"]:
                logger.info(f"[{symbol}] 剔除: 動態趨勢濾網偵測到空頭強勢 ({strategy})")
                return None
            trend_state = ema_eval.get("trend", "UNKNOWN")
            ema_8, ema_21, dist_21 = ema_eval.get("ema_8", 0.0), ema_eval.get("ema_21", 0.0), ema_eval.get("distance_from_21", 0.0)
        else:
            trend_state = "N/A"
            ema_8, ema_21, dist_21 = 0.0, 0.0, 0.0

        mmm_pct, safe_lower, safe_upper, days_to_earnings = await _calculate_mmm(ticker, price, today, symbol, is_etf)
        ts_ratio, ts_state = await asyncio.to_thread(_calculate_term_structure, ticker, expirations, price, today)

        # ------------------ VIX306 Advanced Volatility Filters ------------------
        vix_vts_data = await market_data_service.get_vix_term_structure()
        vix_zscores = await market_data_service.get_vix_zscores()
        
        vts_ratio = vix_vts_data.get('vts_ratio', 1.0)
        z30 = vix_zscores.get('zscore_30', 0.0)
        z60 = vix_zscores.get('zscore_60', 0.0)
        
        # Filter #12: VTS Filter
        if strategy == "STO_PUT" and vts_ratio >= 1.0:
            logger.info(f"[{symbol}] 剔除: VIX 目前處於逆價差 Backwardation (VTS >= 1.0)，市場風險極高")
            return None
            
        # Filter #14: Regime Alignment
        vix_trending_up = (z30 > 0.5 and z60 > 0.0)
        is_bullish_strategy = strategy in ["BTO_CALL", "STO_PUT"]
        if vix_trending_up and is_bullish_strategy:
            logger.info(f"[{symbol}] 剔除: VIX 30/60 雙重指標看漲中(波動率放大)，拒絕作多")
            return None
            
        # Filter: SPX/NDX Alignment
        if is_bullish_strategy:
            spy_sma20 = await market_data_service.get_sma("SPY", 20)
            if spy_sma20 and spy_price_val < spy_sma20:
                logger.info(f"[{symbol}] 剔除: SPY 跌破 20MA，大盤弱勢拒絕作多")
                return None
        # -------------------------------------------------------------------------

        if opt_chain is not None:
            vertical_skew, skew_state, is_high_tail_risk = _calculate_vertical_skew(opt_chain, price, days_to_expiry, strategy, symbol, dividend_yield)
            if vertical_skew is None: return None
        else: 
            vertical_skew, skew_state, is_high_tail_risk = 1.0, "N/A", False

        risk_metrics = _validate_risk_and_liquidity(strategy, best_contract, price, indicators.get('hv_current', 0.0), days_to_expiry, symbol)
        if not risk_metrics: return None

        # Filter #13: Tail Risk Filter (1/4-Kelly Adjustment)
        kelly_fraction = 0.25 if is_high_tail_risk else 0.50

        aroc, alloc_pct, margin_per_contract = _calculate_sizing(
            strategy, best_contract, days_to_expiry, 
            expected_move=risk_metrics.get('expected_move', 0.0), 
            price=price, stock_cost=stock_cost, kelly_fraction=kelly_fraction,
            kelly_fraction_override=vix_kelly_override
        )

        # VIX 倉位縮放：將階梯乘數套用至 alloc_pct
        if vix_sizing_multiplier != 1.0:
            alloc_pct *= vix_sizing_multiplier
        if (strategy in ["STO_PUT", "STO_CALL"] and aroc < 15.0) or (strategy in ["BTO_CALL", "BTO_PUT"] and aroc < 30.0): return None

        raw_delta = best_contract.get('bs_delta', 0.0)
        safe_spy_price = spy_price_val if spy_price_val > 0 else 1.0
        weighted_delta = round(raw_delta * beta * (price / safe_spy_price) * 100, 2)

        greeks = calculate_greeks(opt_type, price, best_contract.get('strike', 0.0), max(days_to_expiry, 1) / 365.0, best_contract.get('impliedVolatility', 0.0), dividend_yield)
        
        return {
            "symbol": symbol, "price": price, "beta": beta, "weighted_delta": weighted_delta, "stock_cost": stock_cost,
            "rsi": indicators.get('rsi', 0.0), "sma20": indicators.get('sma20', 0.0), "hv_rank": indicators.get('hv_rank', 0.0),
            "ts_ratio": ts_ratio, "ts_state": ts_state, "v_skew": vertical_skew, "v_skew_state": skew_state,
            "vix_vts_ratio": vts_ratio, "vix_regime": vix_vts_data.get('vts_state', 'UNKNOWN'),
            "vix_z30": z30, "vix_z60": z60, "is_high_tail_risk": is_high_tail_risk,
            "earnings_days": days_to_earnings, "mmm_pct": mmm_pct, "safe_lower": safe_lower, "safe_upper": safe_upper,
            "expected_move": risk_metrics.get('expected_move', 0.0), "em_lower": risk_metrics.get('em_lower', 0.0),
            "em_upper": risk_metrics.get('em_upper', 0.0), "strategy": strategy, "target_date": target_expiry_date,
            "dte": days_to_expiry, "strike": best_contract.get('strike', 0.0), "bid": risk_metrics.get('bid', 0.0),
            "ask": risk_metrics.get('ask', 0.0), "spread": risk_metrics.get('spread', 0.0), "spread_ratio": risk_metrics.get('spread_ratio', 0.0), "delta": raw_delta,
            "iv": best_contract.get('impliedVolatility', 0.0), "aroc": aroc, "alloc_pct": alloc_pct, "margin_per_contract": margin_per_contract,
            "vrp": risk_metrics.get('vrp', 0.0), "theta": round(greeks.get('theta', 0.0), 4), "gamma": round(greeks.get('gamma', 0.0), 6),
            "mid_price": risk_metrics.get('mid_price', 0.0), "suggested_hedge_strike": risk_metrics.get('suggested_hedge_strike'),
            "liq_status": risk_metrics.get('liq_status', 'N/A'), "liq_msg": risk_metrics.get('liq_msg', ''), "spy_price": safe_spy_price,
            "ema_8": ema_8, "ema_21": ema_21, "trend": trend_state, "distance_from_21": dist_21,
            # VIX 戰情階梯元資料
            "vix_spot": vix_spot, "vix_tier_name": vix_tier.get('name', 'N/A'),
            "vix_tier_emoji": vix_tier.get('emoji', ''), "vix_tier_color": vix_tier.get('color_hex', 0x808080),
            "vix_sizing_multiplier": vix_sizing_multiplier, "vix_sto_delta_cap": vix_tier.get('sto_delta_cap', 0.0),
        }
    except Exception as e:
        logger.error(f"分析 {symbol} 錯誤: {e}")
        return None

async def get_option_metrics(symbol, opt_type, strike, expiry):
    ticker = yf.Ticker(symbol)
    today = datetime.now().date()
    try:
        opt_chain = await asyncio.to_thread(ticker.option_chain, expiry)
        chain_data = opt_chain.calls if opt_type == "call" else opt_chain.puts
        contract = chain_data[chain_data['strike'] == strike]
        if contract.empty: return {'delta': 0.0, 'dte': 0, 'mid': 0.0}
        
        c = contract.iloc[0]
        days_to_expiry = (datetime.strptime(expiry, '%Y-%m-%d').date() - today).days
        quote = await market_data_service.get_quote(symbol)
        price = quote.get('c', 0.0) if quote else 0.0
        
        delta_val = calculate_contract_delta(c, price, max(days_to_expiry, 1) / 365.0, ('c' if opt_type == "call" else 'p'))
        bid, ask = c.get('bid', 0.0), c.get('ask', 0.0)
        mid = (bid + ask) / 2.0 if bid > 0 and ask > 0 else c.get('lastPrice', 0.0)
        return {'delta': delta_val, 'dte': days_to_expiry, 'mid': mid}
    except Exception as e:
        logger.error(f"get_option_metrics error for {symbol}: {e}")
        return {'delta': 0.0, 'dte': 0, 'mid': 0.0}

async def find_best_contract(symbol, strategy_type, target_delta, min_dte, max_dte):
    try:
        ticker = yf.Ticker(symbol)
        expirations = await asyncio.to_thread(lambda: ticker.options)
        today = datetime.now().date()
        target_expiry_date, days_to_expiry = _find_target_expiry(expirations, today, min_dte, max_dte)
        if not target_expiry_date: return None
            
        quote = await market_data_service.get_quote(symbol)
        price = quote.get('c', 0.0) if quote else 0.0
        
        opt_type = "call" if "CALL" in strategy_type else "put"
        best_contract, _ = await asyncio.to_thread(_get_best_contract_data, ticker, target_expiry_date, opt_type, target_delta, price, days_to_expiry)
        if best_contract is None: return None
        
        bid, ask = best_contract.get('bid', 0.0), best_contract.get('ask', 0.0)
        mid = (bid + ask) / 2.0 if bid > 0 and ask > 0 else best_contract.get('lastPrice', 0.0)
        return {'strike': float(best_contract.get('strike', 0.0)), 'expiry': target_expiry_date, 'mid': mid}
    except Exception as e:
        logger.error(f"find_best_contract error for {symbol}: {e}")
        return None
