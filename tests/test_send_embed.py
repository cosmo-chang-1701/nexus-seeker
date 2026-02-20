import sys
import os
import asyncio
import discord
import logging

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dotenv import load_dotenv
from cogs.embed_builder import create_scan_embed
from config import DISCORD_TOKEN, DISCORD_ADMIN_USER_ID

logging.basicConfig(level=logging.INFO)

load_dotenv()

async def main():
    if not DISCORD_TOKEN:
        print("❌ Error: No DISCORD_TOKEN found in .env")
        return

    intents = discord.Intents.default()
    client = discord.Client(intents=intents)

    @client.event
    async def on_ready():
        print(f"✅ Logged in as {client.user} (ID: {client.user.id})")
        
        target_user_id = DISCORD_ADMIN_USER_ID
        try:
            user = await client.fetch_user(target_user_id)
        except Exception as e:
            print(f"❌ Failed to fetch user: {e}")
            await client.close()
            return
            
        if not user:
            print(f"❌ Could not find user with ID {target_user_id}")
            await client.close()
            return

        print(f"🚀 Sending embed to user: {user.name} (ID: {user.id})")
        
        # Mock Data for Embed
        mock_data = {
            "strategy": "STO_PUT", "symbol": "AAPL", "target_date": "2026-03-20",
            "strike": 150.0, "aroc": 12.5, "price": 160.0, "rsi": 45.2,
            "sma20": 158.0, "hv_rank": 60, "vrp": 0.05, "delta": -0.25,
            "iv": 0.45, "ts_ratio": 1.1, "ts_state": "Contango",
            "v_skew": 1.15, "v_skew_state": "Normal", "alloc_pct": 0.1,
            "margin_per_contract": 2500, "expected_move": 10.0,
            "em_lower": 150.0, "em_upper": 170.0, "bid": 2.5, "ask": 2.8,
            "mid_price": 2.65, "spread": 0.3, "spread_ratio": 11.3,
            "liq_status": "🟢 良好", "liq_msg": "流動性充足",
            "earnings_days": 10, "safe_lower": 145.0, "safe_upper": 175.0,
            "mmm_pct": 8.0, "suggested_hedge_strike": 145.0
        }
        
        embed = create_scan_embed(mock_data, user_capital=100000.0)
        
        try:
            await user.send(embed=embed)
            print("✅ Embed sent successfully!")
        except Exception as e:
            print(f"❌ Failed to send embed: {e}")
            
        await client.close()

    try:
        async with client:
            await client.start(DISCORD_TOKEN)
    except Exception as e:
        print(f"❌ Connection failed: {e}")

if __name__ == "__main__":
    asyncio.run(main())