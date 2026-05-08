import sqlite3
import sys
import os
from datetime import datetime
import asyncio
import logging

# Add paths
sys.path.append(os.getcwd())
sys.path.append(os.path.join(os.getcwd(), 'nexus_core'))

from nexus_core.market_analysis.portfolio import refresh_portfolio_greeks, get_option_chain_mid_iv
from nexus_core.database.user_settings import get_full_user_context

async def verify():
    db_path = 'nexus_core/data/nexus_data.db'
    conn = sqlite3.connect(db_path)
    cursor = conn.cursor()
    
    print("--- 🕵️ Greeks Aggregation Audit ---")
    
    # 1. Find the MU trade
    cursor.execute("SELECT id, user_id, symbol, opt_type, strike, expiry, quantity, theta FROM portfolio WHERE symbol='MU'")
    mu_trades = cursor.fetchall()
    
    if not mu_trades:
        print("❌ Error: No MU trade found in 'portfolio' table.")
        # Try virtual trades
        cursor.execute("SELECT id, user_id, symbol, opt_type, strike, expiry, quantity, theta, status FROM virtual_trades WHERE symbol='MU'")
        mu_trades = cursor.fetchall()
        if mu_trades:
            print(f"✅ Found MU trade in 'virtual_trades' table: {mu_trades}")
        else:
            print("❌ Error: MU trade not found in 'virtual_trades' either.")
            return
    else:
        print(f"✅ Found MU trade in 'portfolio' table: {mu_trades}")

    trade = mu_trades[0]
    tid, uid, sym, opt_t, strike, expiry, qty, stored_theta = trade[:8]
    
    # 2. Test Refresh Logic
    print(f"\n--- Testing Refresh Logic for {sym} ---")
    await refresh_portfolio_greeks(uid)
    
    # Check if updated
    cursor.execute(f"SELECT theta FROM {'portfolio' if len(trade)==8 else 'virtual_trades'} WHERE id=?", (tid,))
    new_theta = cursor.fetchone()[0]
    print(f"Stored Annual Theta: {stored_theta} -> {new_theta}")
    
    # 3. Check Aggregation
    print("\n--- Testing Aggregation Layer ---")
    ctx = get_full_user_context(uid)
    print(f"User Context Total Theta (Daily): ${ctx.total_theta:.4f}")
    
    if ctx.total_theta > 0:
        print("\n✅ Verification SUCCESS: Daily Theta is positive.")
    else:
        # Check if it was skipped
        mid, iv = get_option_chain_mid_iv(sym, expiry, strike, opt_t)
        print(f"Market Data: Mid=${mid}, IV={iv:.2%}")
        if iv <= 0 and mid <= 0:
            print("❌ Failure: Missing market data (IV & Price). Theta cannot be calculated.")
        elif new_theta == 0:
            print("❌ Failure: Theta calculation returned 0.0 despite market data.")
        else:
            print("❌ Failure: Aggregation logic returned 0.0.")

if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    asyncio.run(verify())
