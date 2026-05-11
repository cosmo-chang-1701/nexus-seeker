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
    additional_capital_required: float # 追加保證金/現金
    net_capital_outlay: float         # 淨投入資本 (扣除權利金)
    adjusted_cost_basis: float        # 調整後每股成本
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
    """
    # 1. 計算平倉收益
    net_proceeds = current_option_pnl

    # 2. 計算購入現股所需總資本
    total_purchase_cost = current_stock_price * lot_size
    cc_total_premium = target_cc_premium * lot_size

    # 3. 計算追加資本 (不含即將收取的權利金，因為下單買股票時權利金尚未入帳)
    additional_capital = max(0.0, total_purchase_cost - net_proceeds)

    # 4. 淨投入資本 (扣除期權利潤與即將收取的權利金)
    net_capital_outlay = total_purchase_cost - net_proceeds - cc_total_premium

    # 5. 計算調整後成本價 (Adjusted Cost Basis)
    adjusted_cost_basis = net_capital_outlay / lot_size

    # 6. 計算預期年化回報率 (AROC)
    # 使用淨投入作為分母來衡量資本效率
    projected_aroc = (cc_total_premium / net_capital_outlay * (365 / dte) * 100) if net_capital_outlay > 0 else 0.0

    # 7. 效率增益：相對於現價的成本折讓比
    efficiency_gain = (1 - (adjusted_cost_basis / current_stock_price)) * 100

    return TransitionResult(
        initial_pnl=current_option_pnl,
        net_proceeds=net_proceeds,
        shares_purchasable=lot_size,
        additional_capital_required=additional_capital,
        net_capital_outlay=net_capital_outlay,
        adjusted_cost_basis=adjusted_cost_basis,
        total_shares=lot_size,
        cc_strike=target_cc_strike,
        cc_premium=target_cc_premium,
        projected_aroc=projected_aroc,
        capital_efficiency_gain=efficiency_gain
    )

def calculate_survival_runway(cash_reserve: float, monthly_expense: float, daily_theta: float) -> float:
    """
    生存天數計算 (Survival Runway):
    衡量現有現金儲備配合 Theta 現金流能維持多久的生存。
    """
    net_monthly_burn = monthly_expense - (daily_theta * 30)
    if net_monthly_burn <= 0:
        return 9999.0
    runway_months = cash_reserve / net_monthly_burn
    return round(runway_months * 30, 1)

# Aliases for compatibility
calculate_financial_runway = calculate_survival_runway

def simulate_cc_transition(
    current_option_pnl: float,
    current_stock_price: float,
    target_cc_strike: float,
    target_cc_premium: float,
    lot_size: int = 100
) -> TransitionResult:
    """
    向前相容的 CC 演進模擬接口。
    """
    return simulate_pro_transition(
        current_option_pnl=current_option_pnl,
        current_stock_price=current_stock_price,
        target_cc_strike=target_cc_strike,
        target_cc_premium=target_cc_premium,
        lot_size=lot_size
    )
