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
