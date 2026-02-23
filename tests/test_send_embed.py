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
        
        # Base Mock Data
        base_data = {
            "target_date": "2026-03-20",
            "aroc": 12.5, "price": 160.0, "rsi": 45.2,
            "sma20": 158.0, "hv_rank": 60, "vrp": 0.05, "delta": -0.25,
            "iv": 0.45, "ts_ratio": 1.1, "ts_state": "Contango",
            "v_skew": 1.15, "v_skew_state": "Normal", "alloc_pct": 0.1,
            "margin_per_contract": 2500, "expected_move": 10.0,
            "em_lower": 150.0, "em_upper": 170.0, "bid": 2.5, "ask": 2.8,
            "mid_price": 2.65, "spread": 0.3, "spread_ratio": 11.3,
            "liq_status": "🟢 良好", "liq_msg": "流動性充足",
            "earnings_days": 10, "safe_lower": 145.0, "safe_upper": 175.0,
            "mmm_pct": 8.0
        }

        # 所有的可能情境：
        # 1. STO_PUT (Sell To Open Put)
        # 2. STO_CALL (Naked Sell To Open Call)
        # 3. Covered Call (STO_CALL with stock_cost > 0)
        # 4. BTO_CALL (Buy To Open Call, 可帶 hedge)
        # 5. BTO_PUT (Buy To Open Put, 可帶 hedge)
        # 6. 無財報週期影響 (earnings_days > 14) 或波動率正常
        # 7. Edge cases 例如 alloc_pct <= 0 等 (附加測試)
        scenarios = [
            {"strategy": "STO_PUT", "symbol": "AAPL-STO_P", "strike": 150.0, **base_data, "ai_decision": "APPROVE", "ai_reasoning": "技術面與基本面良好，建議執行交易。", "news_text": "Apple 推出新產品，市場反應熱烈。", "reddit_context": "r/options: Apple STO_PUT is free money."},
            {"strategy": "STO_CALL", "symbol": "AAPL-STO_C", "strike": 170.0, **base_data, "ai_decision": "VETO", "ai_reasoning": "財報即將公布，波動率過高，不建議裸賣 Call。"},
            {"strategy": "STO_CALL", "symbol": "AAPL-CC", "strike": 170.0, "stock_cost": 155.0, **base_data, "ai_decision": "SKIP", "ai_reasoning": "未啟用 AI 驗證。"},
            {"strategy": "BTO_CALL", "symbol": "AAPL-BTO_C", "strike": 165.0, "suggested_hedge_strike": 175.0, **base_data},
            {"strategy": "BTO_PUT", "symbol": "AAPL-BTO_P", "strike": 155.0, "suggested_hedge_strike": 145.0, **base_data},
            {"strategy": "STO_PUT", "symbol": "AAPL-NoEarn", "strike": 150.0, **{**base_data, "earnings_days": 20, "ts_ratio": 1.0, "v_skew": 1.1}},
            {"strategy": "STO_PUT", "symbol": "AAPL-LowCapital", "strike": 150.0, **{**base_data, "alloc_pct": 0}},
        ]
        
        for idx, mock_data in enumerate(scenarios, 1):
            embed = create_scan_embed(mock_data, user_capital=100000.0)
            try:
                await user.send(embed=embed)
                print(f"✅ [{idx}/{len(scenarios)}] Embed sent successfully for scenario: {mock_data['strategy']} ({mock_data.get('symbol', '')})!")
                await asyncio.sleep(1) # 避免 Rate limit
            except Exception as e:
                print(f"❌ Failed to send embed for {mock_data['strategy']}: {e}")
            
        await client.close()

    try:
        async with client:
            await client.start(DISCORD_TOKEN)
    except Exception as e:
        print(f"❌ Connection failed: {e}")

if __name__ == "__main__":
    asyncio.run(main())