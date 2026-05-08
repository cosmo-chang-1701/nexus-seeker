import sys
import os
from datetime import datetime
import pandas as pd

# Add the current directory to sys.path
sys.path.append(os.getcwd())
sys.path.append(os.path.join(os.getcwd(), 'nexus_core'))

from nexus_core.market_analysis.greeks import calculate_greeks
from nexus_core.config import RISK_FREE_RATE

def repro():
    # Trade ID 14: MU, Strike 610, Expiry 2026-05-29, STO 1 contract
    # Current Date: 2026-05-08
    today = datetime(2026, 5, 8)
    expiry = datetime(2026, 5, 29)
    t_days = (expiry - today).days
    t_years = max(t_days, 1) / 365.0
    
    # MU price ~ $120
    # Deep ITM Put
    stock_price = 120.0
    strike = 610.0
    iv = 0.25 # Typical IV
    q = 0.015  # Dividend yield
    
    greeks = calculate_greeks('put', stock_price, strike, t_years, iv, q)
    print(f"Calculated Greeks for Long Put: {greeks}")
    
    qty = -1
    annual_dollar_theta = greeks['theta'] * qty * 100
    daily_dollar_theta = annual_dollar_theta / 365.0
    
    print(f"Annualized Dollar Theta (for STO 1): ${annual_dollar_theta:.4f}")
    print(f"Daily Dollar Theta (for STO 1): ${daily_dollar_theta:.4f}")

if __name__ == "__main__":
    repro()
