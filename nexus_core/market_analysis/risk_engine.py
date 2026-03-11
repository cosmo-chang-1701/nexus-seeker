import math
import logging
import asyncio
import numpy as np
import pandas as pd
from dataclasses import dataclass
from services import market_data_service
from datetime import datetime
from typing import Dict, List, Tuple, Any, Optional

logger = logging.getLogger(__name__)

@dataclass
class MacroContext:
    vix: float
    oil_price: float
    vix_change: float

def evaluate_defense_status(quantity: float, opt_type: str, pnl_pct: float, current_delta: float, dte: int) -> str:
    if quantity < 0: 
        if pnl_pct >= 0.50: return "✅ **建議停利** ｜ 獲利達 50% (Buy to Close)"
        if pnl_pct <= -1.50: return "☠️ **強制停損** ｜ 虧損達 150% (黑天鵝警戒)"
        if opt_type == 'put' and current_delta <= -0.40: return "🚨 **動態轉倉** ｜ Put Delta 擴張 (Roll Down & Out)"
        if opt_type == 'call' and current_delta >= 0.40: return "🚨 **動態轉倉** ｜ Call Delta 擴張 (Roll Up & Out)"
        if dte <= 21: return "⚠️ **Gamma 陷阱** ｜ DTE ≤ 21 (建議平倉或轉倉)"
    else:
        if pnl_pct >= 1.0: return "✅ **建議停利** ｜ 獲利達 100% (Sell to Close)"
        if pnl_pct <= -0.50: return "⚠️ **停損警戒** ｜ 本本金回撤達 50%"
        if dte <= 21: return "🚨 **動能衰竭** ｜ DTE ≤ 21 (建議平倉保留殘值)"
    return "⏳ **繼續持有** ｜ 未達防禦觸發條件"

def calculate_beta(df_stock: pd.DataFrame, df_spy: pd.DataFrame) -> float:
    try:
        combined = pd.merge(df_stock['Close'], df_spy['Close'], left_index=True, right_index=True, how='inner').dropna()
        if len(combined) < 60: return 1.0
        log_returns = np.log(combined / combined.shift(1)).dropna()
        cov_matrix = np.cov(log_returns.iloc[:, 0], log_returns.iloc[:, 1])
        beta = cov_matrix[0, 1] / cov_matrix[1, 1]
        return round(float(np.clip(beta, -5.0, 5.0)), 2)
    except Exception: return 1.0

async def analyze_sector_correlation(symbols: List[str]) -> List[Tuple[str, str, float]]:
    """計算板塊非系統性集中風險 (Async)。"""
    if len(symbols) <= 1: return []
    try:
        dfs = {}
        tasks = {sym: market_data_service.get_history_df(sym, "60d") for sym in symbols}
        results = await asyncio.gather(*tasks.values())
        for sym, df in zip(tasks.keys(), results):
            if not df.empty: dfs[sym] = df['Close']
        if len(dfs) <= 1: return []
        hist_data = pd.DataFrame(dfs)
        returns = hist_data.pct_change().dropna()
        corr_matrix = returns.corr()
        high_corr_pairs = []
        for i in range(len(corr_matrix.columns)):
            for j in range(i+1, len(corr_matrix.columns)):
                rho = corr_matrix.iloc[i, j]
                if rho > 0.75: high_corr_pairs.append((corr_matrix.columns[i], corr_matrix.columns[j], float(rho)))
        return high_corr_pairs
    except Exception as e:
        logger.error(f"相關性矩陣運算失敗: {e}")
        return []

def simulate_exposure_impact(current_total_delta: float, new_trade_data: Dict[str, Any], user_capital: float, spy_price: float, suggested_contracts: int = 1) -> Tuple[float, float]:
    strategy = new_trade_data.get('strategy', '')
    side_multiplier = -1 if "STO" in strategy else 1
    new_trade_weighted_delta = new_trade_data.get('weighted_delta', 0.0) * side_multiplier * suggested_contracts
    projected_total_delta = current_total_delta + new_trade_weighted_delta
    projected_exposure_pct = (projected_total_delta * spy_price) / user_capital * 100 if user_capital > 0 else 0
    return projected_total_delta, projected_exposure_pct

def get_macro_modifiers(macro: MacroContext) -> Tuple[float, float]:
    w_vix = 1.2 if macro.vix < 15 else 1.0 if macro.vix < 20 else 0.8 if macro.vix < 25 else 0.6 if macro.vix < 35 else 0.4
    w_oil = 1.0 if macro.oil_price < 75 else 0.9 if macro.oil_price < 85 else 0.7 if macro.oil_price < 95 else 0.5
    return w_vix, w_oil

def optimize_position_risk(current_delta: float, unit_weighted_delta: float, user_capital: float, spy_price: float, stock_iv: float, strategy: str, macro_data: Optional[MacroContext] = None, base_risk_limit_pct: float = 15.0) -> Tuple[int, float]:
    if spy_price <= 0:
        return 0, 0.0

    spy_iv, risk_limit_pct = 0.16, base_risk_limit_pct
    if macro_data: d_vix, d_oil = get_macro_modifiers(macro_data); risk_limit_pct = base_risk_limit_pct * d_vix * d_oil; spy_iv = macro_data.vix / 100.0
    val_adj_unit_delta = unit_weighted_delta * (stock_iv / max(spy_iv, 0.01)) * (-1 if "STO" in strategy else 1)
    max_safe_shares = (user_capital * (risk_limit_pct / 100)) / spy_price
    safe_qty = int((max_safe_shares - current_delta) // val_adj_unit_delta) if val_adj_unit_delta > 0 else int((-max_safe_shares - current_delta) // val_adj_unit_delta) if val_adj_unit_delta < 0 else 0
    proj_delta = current_delta + (val_adj_unit_delta * max(0, safe_qty))
    suggested_hedge_spy = proj_delta - max_safe_shares if proj_delta > max_safe_shares else proj_delta + max_safe_shares if proj_delta < -max_safe_shares else 0.0
    return max(0, safe_qty), round(float(suggested_hedge_spy), 2)

def get_macro_risk_metrics(total_beta_delta: float, total_theta: float, total_margin_used: float, total_gamma: float, user_capital: float, spy_price: float) -> Dict[str, Any]:
    net_exposure_dollars = total_beta_delta * spy_price
    exposure_pct = (net_exposure_dollars / user_capital) * 100 if user_capital > 0 else 0
    return {"net_exposure_dollars": net_exposure_dollars, "exposure_pct": exposure_pct, "total_beta_delta": total_beta_delta, "gamma_threshold": (user_capital / 10000.0) * 2.0, "theta_yield": (total_theta / user_capital) * 100 if user_capital > 0 else 0, "portfolio_heat": (total_margin_used / user_capital) * 100 if user_capital > 0 else 0, "total_gamma": total_gamma, "total_theta": total_theta, "total_margin_used": total_margin_used}
