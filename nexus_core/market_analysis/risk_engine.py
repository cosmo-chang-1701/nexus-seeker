import math
import logging
import numpy as np
import pandas as pd
from services import market_data_service
from datetime import datetime
from typing import Dict, List, Tuple, Any

logger = logging.getLogger(__name__)

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
    使用對數收益率計算標的與基準 (SPY) 的 Beta 係數。
    公式: 
    1. Log Returns: r_t = ln(P_t / P_{t-1})
    2. Beta: \beta = \frac{Cov(r_i, r_m)}{Var(r_m)}
    """
    try:
        if df_stock is None or df_spy is None or df_stock.empty or df_spy.empty:
            return 1.0
            
        # 1. 對齊日期並僅取 Close 價格
        combined = pd.merge(
            df_stock['Close'], 
            df_spy['Close'], 
            left_index=True, 
            right_index=True, 
            how='inner', 
            suffixes=('_stock', '_spy')
        ).dropna()
        
        # 樣本數門檻 (60 交易日)
        if len(combined) < 60:
            return 1.0
            
        # 2. 計算對數收益率 (Log Returns)
        # log(P_t / P_{t-1}) 等同於 log(P_t) - log(P_{t-1})
        log_returns = np.log(combined / combined.shift(1)).replace([np.inf, -np.inf], np.nan).dropna()
        
        if len(log_returns) < 50:
            return 1.0
            
        # 3. 計算協方差與方差
        # 使用 numpy.cov 提取協方差矩陣中的關鍵值
        cov_matrix = np.cov(log_returns['Close_stock'], log_returns['Close_spy'])
        covariance = cov_matrix[0, 1]
        variance = cov_matrix[1, 1]
        
        if variance < 1e-9:
            return 1.0
            
        # 4. 產出 Beta 並套用限制器
        beta = covariance / variance
        beta = np.clip(beta, -5.0, 5.0)
        
        return round(float(beta), 2)
        
    except Exception as e:
        logger.error(f"Log Return Beta 計算失敗: {e}")
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

def optimize_position_risk(
    current_delta: float, 
    unit_weighted_delta: float, 
    user_capital: float, 
    spy_price: float, 
    stock_iv: float,
    spy_iv: float,  # 通常使用 VIX 或 SPY 30D IV
    risk_limit_pct: float = 15.0, 
    strategy: str = ""
) -> Tuple[int, float]:
    """
    計算「波動率校正後」的安全成交口數與對沖建議。
    """
    # 1. 計算基本的風險空間 (基於資金規模)
    max_safe_shares = (user_capital * (risk_limit_pct / 100)) / spy_price
    side_multiplier = -1 if "STO" in strategy else 1
    
    # 2. 引入【波動率調節因子 (Vol Multiplier)】
    # 如果標的 IV 高於 SPY IV，則放大該部位的風險權重，採取更保守的對沖
    # 公式：IV_Adjustment = Stock_IV / SPY_IV
    iv_adjustment_ratio = stock_iv / spy_iv if spy_iv > 0 else 1.0
    
    # 校正後的單位加權 Delta (Forward-looking Delta)
    # 這讓高 IV 標的在系統中看起來比實際上「更重」
    vol_adjusted_unit_delta = unit_weighted_delta * iv_adjustment_ratio * side_multiplier
    
    # 3. 計算安全口數 (以校正後的曝險計算)
    safe_qty = 0
    if vol_adjusted_unit_delta > 0:
        room = max_safe_shares - current_delta
        safe_qty = math.floor(room / vol_adjusted_unit_delta) if room > 0 else 0
    elif vol_adjusted_unit_delta < 0:
        room = -max_safe_shares - current_delta
        safe_qty = math.floor(room / vol_adjusted_unit_delta) if room < 0 else 0

    safe_qty = max(0, safe_qty)
    
    # 4. 精算【SPY 對沖建議】
    # 這裡的建議會更具「前瞻性」，防禦 IV 飆升導致的 Delta 爆炸
    suggested_hedge_spy = 0.0
    projected_delta = current_delta + (unit_weighted_delta * side_multiplier * max(1, safe_qty))
    
    # 根據波動率係數進一步強化對沖力道
    if projected_delta > max_safe_shares:
        suggested_hedge_spy = (projected_delta - max_safe_shares) * iv_adjustment_ratio
    elif projected_delta < -max_safe_shares:
        suggested_hedge_spy = (projected_delta - (-max_safe_shares)) * iv_adjustment_ratio
        
    return safe_qty, round(float(suggested_hedge_spy), 1)

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
