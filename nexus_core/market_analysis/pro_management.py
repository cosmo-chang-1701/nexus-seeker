from dataclasses import dataclass
from typing import Dict, Any

@dataclass
class TransitionResult:
    """
    Data structure representing the outcome of a position transition simulation.
    """
    initial_pnl: float
    net_capital_outlay: float
    adjusted_cost_basis: float
    total_shares: int
    cc_strike: float
    cc_premium: float
    projected_aroc: float
    capital_efficiency_gain: float

from dataclasses import dataclass
from typing import Dict, Any

@dataclass
class TransitionResult:
    """
    Data structure representing the outcome of a position transition simulation.
    """
    initial_pnl: float
    net_proceeds: float
    shares_purchasable: int
    additional_capital_required: float
    adjusted_cost_basis: float
    total_shares: int
    cc_strike: float
    cc_premium: float
    projected_aroc: float
    capital_efficiency_gain: float

def simulate_pro_transition(
    current_option_pnl: float,
    current_stock_price: float,
    target_cc_strike: float,
    target_cc_premium: float,
    lot_size: int = 100,
    dte: int = 30
) -> TransitionResult:
    """
    戰略轉軌引擎 (Strategic Transition Engine)：
    模擬將「投機性期權部位 (Speculative Options)」演進為「核心現股 + 備兌買權 (Core Equity + Covered Call)」的過程。
    
    此引擎計算平倉 DITM 期權後的淨收益，以及轉換為現股所需的追加資本。
    """
    # 1. 計算平倉收益 (假設 1 口合約 = 100 股)
    net_proceeds = current_option_pnl 
    
    # 2. 計算購入 100 股所需總資本
    total_purchase_cost = current_stock_price * lot_size
    
    # 3. 計算追加資本
    additional_capital = max(0.0, total_purchase_cost - net_proceeds)
    
    # 4. 計算調整後成本價 (Adjusted Cost Basis)
    # (總買入成本 - 期權已實現利潤) / 股數
    adjusted_cost_basis = (total_purchase_cost - net_proceeds) / lot_size
    
    # 5. 計算預期年化回報率 (AROC)
    # AROC = (Premium / Margin) * (365 / DTE)
    # 對於 Covered Call，Margin 等於現股價格 (或保證金要求)，此處以現股價格計
    margin = current_stock_price * lot_size
    projected_aroc = (target_cc_premium * lot_size / margin) * (365 / dte) * 100
    
    # 6. 計算資本效率增益 (相對於單純持有現股)
    efficiency_gain = (target_cc_premium / current_stock_price) * 100

    return TransitionResult(
        initial_pnl=current_option_pnl,
        net_proceeds=net_proceeds,
        shares_purchasable=lot_size,
        additional_capital_required=additional_capital,
        adjusted_cost_basis=adjusted_cost_basis,
        total_shares=lot_size,
        cc_strike=target_cc_strike,
        cc_premium=target_cc_premium,
        projected_aroc=projected_aroc,
        capital_efficiency_gain=efficiency_gain
    )

def calculate_survival_runway(cash_reserve: float, monthly_expenses: float, daily_theta: float) -> float:
    """
    生存天數計算 (Survival Runway):
    Runway (Days) = Cash Reserve / (Monthly Expenses - (Daily Portfolio Theta * 30)) * 30
    
    此公式衡量在不考慮本金增值的情況下，現有現金儲備配合 Theta 現金流能維持多久的生存。
    """
    # 每月淨支出 = 每月預算 - 每月預期 Theta 收益 (假設每月 30 天)
    net_monthly_burn = monthly_expenses - (daily_theta * 30)
    
    # 如果 Theta 收益已經超過每月支出，則生存天數視為無限 (Infinity Fallback)
    if net_monthly_burn <= 0:
        return 9999.0
        
    runway_months = cash_reserve / net_monthly_burn
    return round(runway_months * 30, 1)

def simulate_cc_transition(
    current_option_pnl: float,
    current_stock_price: float,
    target_cc_strike: float,
    target_cc_premium: float,
    lot_size: int = 100
) -> TransitionResult:
    """
    Simulates the transition from a profitable speculative Call/Synthetic position 
    to a Core Equity position with a Covered Call overlay.
    
    Args:
        current_option_pnl (float): Realized profit from closing the existing option position.
        current_stock_price (float): Current market price of the underlying equity.
        target_cc_strike (float): Strike price of the proposed Covered Call.
        target_cc_premium (float): Premium collected from writing the Covered Call.
        lot_size (int): Standardized unit of shares (default 100).
        
    Returns:
        TransitionResult: Quantitative breakdown of the transition.
    """
    # 1. Calculate Gross Cost for 100 shares
    gross_stock_cost = current_stock_price * lot_size
    
    # 2. Net Capital Outlay = Gross Cost - Realized PnL - CC Premium Collected
    cc_total_premium = target_cc_premium * lot_size
    net_capital_outlay = gross_stock_cost - current_option_pnl - cc_total_premium
    
    # 3. Adjusted Cost Basis per share
    adjusted_cost_basis = net_capital_outlay / lot_size
    
    # 4. Calculate Annualized Return on Capital (AROC) for the new CC
    # Assuming standard 30-day DTE for AROC calculation if not specified, 
    # but here we focus on the yield relative to net outlay.
    # Formula: (Premium / Net Outlay) * (365 / DTE)
    # We will use a generic 30-day DTE for the projection as per professional standards.
    dte = 30
    yield_on_outlay = (cc_total_premium / net_capital_outlay) if net_capital_outlay > 0 else 0.0
    projected_aroc = yield_on_outlay * (365 / dte) * 100
    
    # 5. Efficiency Gain: Reduction in cost basis vs market price
    efficiency_gain = (1 - (adjusted_cost_basis / current_stock_price)) * 100

    return TransitionResult(
        initial_pnl=current_option_pnl,
        net_capital_outlay=net_capital_outlay,
        adjusted_cost_basis=adjusted_cost_basis,
        total_shares=lot_size,
        cc_strike=target_cc_strike,
        cc_premium=target_cc_premium,
        projected_aroc=projected_aroc,
        capital_efficiency_gain=efficiency_gain
    )
