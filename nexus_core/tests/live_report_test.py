import asyncio
import discord
import sys
import os

# 確保路徑包含專案根目錄
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.append(PROJECT_ROOT)
from market_analysis.portfolio import check_portfolio_status_logic
from cogs.embed_builder import create_portfolio_report_embed
from config import DISCORD_TOKEN, DISCORD_ADMIN_USER_ID

async def send_real_report():
    intents = discord.Intents.default()
    client = discord.Client(intents=intents)

    @client.event
    async def on_ready():
        print(f"✅ 已登入: {client.user}")
        user = await client.fetch_user(DISCORD_ADMIN_USER_ID)
        
        # 準備一組假資料來觸發我們優化後的排版
        # (symbol, opt_type, strike, expiry, entry_price, quantity, stock_cost)
        mock_rows = [
            ("AAPL", "put", 145.0, "2026-03-20", 3.20, -1, 0.0),
            ("TSLA", "call", 260.0, "2026-03-20", 5.50, -1, 240.0) # Covered Call demo
        ]
        
        print("📊 正在生成真實報告內容...")
        report_lines = check_portfolio_status_logic(mock_rows, user_capital=100000.0)
        embed = create_portfolio_report_embed(report_lines)
        
        print(f"🚀 正在發送至 Discord (User ID: {DISCORD_ADMIN_USER_ID})...")
        await user.send(content="🔔 **Nexus Seeker 排版優化測試 (真實現場發送)**", embed=embed)
        print("✨ 發送成功！請查看您的 Discord。")
        await client.close()

    await client.start(DISCORD_TOKEN)

if __name__ == "__main__":
    asyncio.run(send_real_report())
