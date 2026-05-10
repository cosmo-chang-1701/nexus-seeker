import math
import logging
import asyncio
import numpy as np
import pandas as pd
from dataclasses import dataclass
from services import market_data_service
from datetime import datetime
from enum import Enum
from typing import Dict, List, Tuple, Any, Optional
from config import get_vix_tier, VIX_QUANTILE_BOUNDS

logger = logging.getLogger(__name__)

class DITMDefenseAction(Enum):
    DEFENSIVE_CLOSE = "DEFENSIVE_CLOSE"
    ROLL_UP_OUT = "ROLL_UP_OUT"
    HOLD = "HOLD"

@dataclass
class MacroContext:
    vix: float
    oil_price: float
    vix_change: float
    vts_ratio: float = 1.0
    vix_trend_up: bool = False

def evaluate_ditm_defense(quantity: float, current_delta: float, dte: int, pnl_pct: float) -> DITMDefenseAction:
    """
    評估 Deep In-The-Money (DITM) 防禦策略。
    
    當長部位進入 DITM 時，其凸性 (Convexity) 消失，Delta 接近 1.0，
    此時風險報酬比惡化（變成類現股但有時間價值損耗），系統應介入獲利了結或轉倉。
    
    Trigger Conditions:
    1. 買方部位 (quantity > 0)
    2. 絕對 Delta >= 0.85
    3. 未實現損益 > 150% (pnl_pct > 1.5)
    4. 距離到期日 <= 21 天
    """
    if quantity <= 0:
        return DITMDefenseAction.HOLD

    abs_delta = abs(current_delta)
    
    if abs_delta >= 0.85 and pnl_pct > 1.5 and dte <= 21:
        # 剩餘天數較短且獲利豐厚，若 Delta 極高則建議轉倉以回收資金並重新獲得槓桿/凸性
        # 如果 DTE 真的非常短 (例如 < 7)，建議直接關閉以規避 Gamma 風險
        if dte <= 7:
            return DITMDefenseAction.DEFENSIVE_CLOSE
        return DITMDefenseAction.ROLL_UP_OUT
        
    return DITMDefenseAction.HOLD

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

def get_macro_modifiers(macro: MacroContext, pcr: float = 0.8, skew: float = 0.0) -> Tuple[float, float, float]:
    """計算宏觀環境風險修正因子。
    
    VIX 戰情階梯邏輯 (攻守互換):
    ...
    """
    if macro.vix >= 35:
        w_vix = 2.0
    elif macro.vix >= 30:
        w_vix = 1.5
    elif macro.vix >= 24:
        w_vix = 1.2
    elif macro.vix >= 18:
        w_vix = 1.0
    elif macro.vix >= 15:
        w_vix = 0.5
    else:
        w_vix = 0.0

    w_oil = 1.0 if macro.oil_price < 75 else 0.9 if macro.oil_price < 85 else 0.7 if macro.oil_price < 95 else 0.5
    w_regime = 0.6 if (macro.vts_ratio >= 1.0 or macro.vix_trend_up) else 1.0
    
    # 整合 PCR 與 Skew 修正
    if pcr > 1.2: w_regime *= 0.8 # PCR 過高，情緒過於悲觀，縮減賣方曝險
    if skew > 10: w_regime *= 0.9 # Skew 過大，尾端風險對沖成本過高
    
    return w_vix, w_oil, w_regime

def optimize_position_risk(current_delta: float, unit_weighted_delta: float, user_capital: float, spy_price: float, stock_iv: float, strategy: str, macro_data: Optional[MacroContext] = None, base_risk_limit_pct: float = 15.0, is_high_tail_risk: bool = False, vix_spot: Optional[float] = None, pcr: float = 0.8, skew: float = 0.0) -> Tuple[int, float]:
    """NRO 風險優化器：根據宏觀環境計算安全持倉口數。
    
    Args:
        vix_spot: VIX 即時價格。用於動態 Kelly 縮放與 All-in 模式。
        pcr: 買賣權比率。
        skew: 期權偏斜度。
    """
    if spy_price <= 0:
        return 0, 0.0

    spy_iv, risk_limit_pct = 0.16, base_risk_limit_pct
    if macro_data: 
        # ---------- VIX < 15.0 Dormant Tier Enforcement ----------
        if macro_data.vix < 15.0 and "STO" in strategy:
            logger.info(f"NRO Reject: VIX {macro_data.vix:.1f} is in Dormant tier. STO entry forbidden.")
            return 0, 0.0
        # --------------------------------------------------------
        
        d_vix, d_oil, d_regime = get_macro_modifiers(macro_data, pcr, skew)
        
        # 整合 PCR 與 Skew 的進一步細節
        if pcr < 0.6 and "BTO" in strategy:
            # 市場過熱，警告但不一定硬拒，此處微幅縮減買方額度
            d_regime *= 0.8
            
        # All-in 模式 (VIX > 35): 繞過宏觀修正因子的衰減效應，
        # 直接使用 base_risk_limit_pct * d_vix 以最大化風險額度。
        if vix_spot is not None and vix_spot >= 35.0:
            risk_limit_pct = base_risk_limit_pct * d_vix  # d_vix=2.0 at this tier
        else:
            risk_limit_pct = base_risk_limit_pct * d_vix * d_oil * d_regime
        spy_iv = macro_data.vix / 100.0

        
    if is_high_tail_risk:
        risk_limit_pct *= 0.5 # Tail risk haircut

    # 動態 Kelly 縮放：VIX 超過 upper_10 (29.5) 時，
    # 從 1/4 Kelly 向 1/2 Kelly 線性插值。
    # 此值透過 strategy 層的 kelly_fraction_override 機制獨立於此處運作，
    # 這裡僅調整 risk_limit_pct 上的 Kelly-adjacent 縮放。
    if vix_spot is not None:
        upper_10 = VIX_QUANTILE_BOUNDS.get('upper_10', 29.5)
        if vix_spot > upper_10:
            # 以 VIX 45 為插值上限 (避免無窮外推)
            vix_ceiling = 45.0
            t = min((vix_spot - upper_10) / (vix_ceiling - upper_10), 1.0)
            kelly_scale = 1.0 + t * 0.5  # 從 1.0x 到 1.5x risk_limit_pct
            risk_limit_pct *= kelly_scale

    val_adj_unit_delta = unit_weighted_delta * (stock_iv / max(spy_iv, 0.01)) * (-1 if "STO" in strategy else 1)
    max_safe_shares = (user_capital * (risk_limit_pct / 100)) / spy_price
    safe_qty = int((max_safe_shares - current_delta) // val_adj_unit_delta) if val_adj_unit_delta > 0 else int((-max_safe_shares - current_delta) // val_adj_unit_delta) if val_adj_unit_delta < 0 else 0
    proj_delta = current_delta + (val_adj_unit_delta * max(0, safe_qty))
    suggested_hedge_spy = proj_delta - max_safe_shares if proj_delta > max_safe_shares else proj_delta + max_safe_shares if proj_delta < -max_safe_shares else 0.0
    return max(0, safe_qty), round(float(suggested_hedge_spy), 2)

def get_macro_risk_metrics(total_beta_delta: float, total_theta: float, total_margin_used: float, total_gamma: float, user_capital: float, spy_price: float, vix_spot: Optional[float] = None, total_vega: float = 0.0, total_vanna: float = 0.0) -> Dict[str, Any]:
    """計算組合級宏觀風險指標。
    
    Args:
        vix_spot: VIX 即時價格。若提供則附帶 VIX 戰情階梯資訊。
        total_vega: 組合總 Vega 曝險。
        total_vanna: 組合總 Vanna 曝險。
    """
    net_exposure_dollars = total_beta_delta * spy_price
    exposure_pct = (net_exposure_dollars / user_capital) * 100 if user_capital > 0 else 0
    
    # VIX 階梯附加資訊
    vix_tier = get_vix_tier(vix_spot) if vix_spot is not None else None
    vix_tier_name = vix_tier['name'] if vix_tier else 'N/A'
    portfolio_heat = (total_margin_used / user_capital) * 100 if user_capital > 0 else 0
    
    # 動態 Portfolio Heat 上限：根據 VIX 階梯調整
    if vix_tier:
        heat_limit = 80.0 * vix_tier.get('sizing_multiplier', 1.0)
    else:
        heat_limit = 80.0  # 預設上限
    
    return {
        "net_exposure_dollars": net_exposure_dollars,
        "exposure_pct": exposure_pct,
        "total_beta_delta": total_beta_delta,
        "gamma_threshold": (user_capital / 10000.0) * 2.0,
        "theta_yield": (total_theta / user_capital) * 100 if user_capital > 0 else 0,
        "portfolio_heat": portfolio_heat,
        "portfolio_heat_limit": heat_limit,
        "total_gamma": total_gamma,
        "total_theta": total_theta,
        "total_vega": total_vega,
        "total_vanna": total_vanna,
        "total_margin_used": total_margin_used,
        "vix_tier_name": vix_tier_name,
    }

def calculate_vega_adjusted_delta(total_delta: float, total_vanna: float, vol_change: float) -> float:
    """
    計算考慮 Vega 影響後的調整後 Delta (Hidden Delta)。
    Delta_adj = Delta + Vanna * Delta_Vol
    """
    return total_delta + (total_vanna * vol_change)

def calculate_hedge_instruction(total_beta_delta: float, hedge_instrument_delta: float = -1.0) -> int:
    """
    計算達成 Delta 中性所需的對沖合約數量。
    N = -Total_Delta / Hedge_Instrument_Delta
    """
    if abs(hedge_instrument_delta) < 0.01:
        return 0
    qty = -total_beta_delta / hedge_instrument_delta
    return int(round(qty))
