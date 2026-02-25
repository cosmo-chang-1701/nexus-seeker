def calculate_option_margin(opt_type: str, strike: float, current_stock_price: float, current_option_price: float, quantity: int, stock_cost: float = 0.0) -> float:
    """
    計算標的部位的保證金佔用量。
    """
    if quantity >= 0:
        return 0.0
        
    abs_qty = abs(quantity)
    
    if opt_type == 'call':
        if stock_cost > 0.0:
            # Covered Call
            return 0.0
        else:
            # Naked Call (簡化版保證金公式)
            otm = max(0, strike - current_stock_price)
            margin_locked = max(
                (0.20 * current_stock_price) - otm + current_option_price, 
                0.10 * current_stock_price + current_option_price
            ) * 100 * abs_qty
            return margin_locked
    elif opt_type == 'put':
        # Cash Secured Put (簡化版：通常為 Strike * 100)
        # 若需要更精確的 Naked Put 公式可比照 Call
        return strike * 100 * abs_qty
        
    return 0.0
