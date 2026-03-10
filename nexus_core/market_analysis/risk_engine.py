import math
import logging
import numpy as np
import pandas as pd
from dataclasses import dataclass
from services import market_data_service
from datetime import datetime
from typing import Dict, List, Tuple, Any, Optional

logger = logging.getLogger(__name__)

@dataclass
class MacroContext:
    """宏觀環境容器"""
    vix: float        # 波動率指數
    oil_price: float  # WTI 原油價格
    vix_change: float # VIX 變動百分比

def evaluate_defense_status(quantity: float, opt_type: str, pnl_pct: float, current_delta: float, dte: int) -> str:
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

def calculate_beta(df_stock: pd.DataFrame, df_spy: pd.DataFrame) -> float:
    r"""
    使用對數收益率 (Log Returns) 計算 Beta。
    公式: $$\beta = \frac{\text{Cov}(\ln(P_i), \ln(P_m))}{\text{Var}(\ln(P_m))}$$
    """
    try:
        combined = pd.merge(
            df_stock['Close'], df_spy['Close'], 
            left_index=True, right_index=True, how='inner'
        ).dropna()
        
        if len(combined) < 60: return 1.0
            
        # 計算 Log Returns
        log_returns = np.log(combined / combined.shift(1)).dropna()
        
        cov_matrix = np.cov(log_returns.iloc[:, 0], log_returns.iloc[:, 1])
        beta = cov_matrix[0, 1] / cov_matrix[1, 1]
        
        return round(float(np.clip(beta, -5.0, 5.0)), 2)
    except Exception:
        return 1.0

def analyze_sector_correlation(symbols: List[str]) -> List[Tuple[str, str, float]]:
    """
    計算板塊非系統性集中風險 (Correlation Matrix)
    回傳高度相關的配對。
    """
    if len(symbols) <= 1:
        return []

    try:
        # 透過 Finnhub 取得各標的的歷史 Close 價格
        dfs = {}
        for sym in symbols:
            df = market_data_service.get_history_df(sym, "60d")
            if not df.empty:
                dfs[sym] = df['Close']
        
        if len(dfs) <= 1:
            return []
        
        hist_data = pd.DataFrame(dfs)
            
        returns = hist_data.pct_change().dropna()
        corr_matrix = returns.corr()

        high_corr_pairs = []
        for i in range(len(corr_matrix.columns)):
            for j in range(i+1, len(corr_matrix.columns)):
                rho = corr_matrix.iloc[i, j]
                if rho > 0.75:
                    high_corr_pairs.append((corr_matrix.columns[i], corr_matrix.columns[j], float(rho)))
        return high_corr_pairs
    except Exception as e:
        logger.error(f"相關性矩陣運算失敗: {e}")
        return []

def simulate_exposure_impact(current_total_delta: float, new_trade_data: Dict[str, Any], user_capital: float, spy_price: float, suggested_contracts: int = 1) -> Tuple[float, float]:
    """
    模擬成交後的總曝險變化。
    """
    strategy = new_trade_data.get('strategy', '')
    side_multiplier = -1 if "STO" in strategy else 1
    new_trade_weighted_delta = new_trade_data.get('weighted_delta', 0.0) * side_multiplier * suggested_contracts
    
    projected_total_delta = current_total_delta + new_trade_weighted_delta
    projected_exposure_dollars = projected_total_delta * spy_price
    projected_exposure_pct = (projected_exposure_dollars / user_capital) * 100 if user_capital > 0 else 0
    
    return projected_total_delta, projected_exposure_pct

def get_macro_modifiers(macro: MacroContext) -> Tuple[float, float]:
    """執行宏觀風險修正矩陣邏輯"""
    # VIX 修正係數 (omega_vix)
    if macro.vix < 15: w_vix = 1.2
    elif macro.vix < 20: w_vix = 1.0
    elif macro.vix < 25: w_vix = 0.8
    elif macro.vix < 35: w_vix = 0.6
    else: w_vix = 0.4
    
    # 原油修正係數 (omega_oil)
    if macro.oil_price < 75: w_oil = 1.0
    elif macro.oil_price < 85: w_oil = 0.9
    elif macro.oil_price < 95: w_oil = 0.7
    else: w_oil = 0.5
    
    return w_vix, w_oil

def optimize_position_risk(
    current_delta: float,
    unit_weighted_delta: float,
    user_capital: float,
    spy_price: float,
    stock_iv: float,
    strategy: str,
    macro_data: Optional[MacroContext] = None,
    base_risk_limit_pct: float = 15.0
) -> Tuple[int, float]:
    """
    量化風險優化引擎 - 整合 IV 校正與宏觀修正
    """
    # 預設基準
    spy_iv = 0.16 
    risk_limit_pct = base_risk_limit_pct

    # 1. 宏觀注入與參數修正
    if macro_data:
        spy_iv = macro_data.vix / 100.0
        w_vix, w_oil = get_macro_modifiers(macro_data)
        risk_limit_pct = base_risk_limit_pct * w_vix * w_oil

    # 2. 前瞻性波動率校正 (IV Scaling)
    iv_adjustment_ratio = stock_iv / max(spy_iv, 0.01)
    
    # 3. 計算動態風險紅線 (以股數為單位)
    max_safe_shares = (user_capital * (risk_limit_pct / 100)) / spy_price
    
    # 4. 判定部位方向並計算校正後 Delta
    side_multiplier = -1 if "STO" in strategy else 1
    # 讓高波標的在計算中佔用更多「虛擬曝險額度」
    vol_adjusted_unit_delta = unit_weighted_delta * iv_adjustment_ratio * side_multiplier
    
    # 5. 安全成交口數計算 (Safe Quantity)
    safe_qty = 0
    if vol_adjusted_unit_delta > 0:
        room = max_safe_shares - current_delta
        safe_qty = int(room // vol_adjusted_unit_delta) if room > 0 else 0
    elif vol_adjusted_unit_delta < 0:
        room = -max_safe_shares - current_delta
        safe_qty = int(room // vol_adjusted_unit_delta) if room < 0 else 0
    
    # 6. 對沖建議精算 (Hedge Suggestion)
    projected_delta = current_delta + (vol_adjusted_unit_delta * max(0, safe_qty))
    suggested_hedge_spy = 0.0
    
    if projected_delta > max_safe_shares:
        suggested_hedge_spy = projected_delta - max_safe_shares
    elif projected_delta < -max_safe_shares:
        suggested_hedge_spy = projected_delta + max_safe_shares
        
    return max(0, safe_qty), round(float(suggested_hedge_spy), 2)

def get_macro_risk_metrics(total_beta_delta: float, total_theta: float, total_margin_used: float, total_gamma: float, user_capital: float, spy_price: float) -> Dict[str, Any]:
    """
    計算宏觀風險指標。
    """
    net_exposure_dollars = total_beta_delta * spy_price
    exposure_pct = (net_exposure_dollars / user_capital) * 100 if user_capital > 0 else 0
    
    gamma_threshold = (user_capital / 10000.0) * 2.0
    theta_yield = (total_theta / user_capital) * 100 if user_capital > 0 else 0
    portfolio_heat = (total_margin_used / user_capital) * 100 if user_capital > 0 else 0
    
    return {
        "net_exposure_dollars": net_exposure_dollars,
        "exposure_pct": exposure_pct,
        "total_beta_delta": total_beta_delta,
        "gamma_threshold": gamma_threshold,
        "theta_yield": theta_yield,
        "portfolio_heat": portfolio_heat,
        "total_gamma": total_gamma,
        "total_theta": total_theta,
        "total_margin_used": total_margin_used
    }
