import discord
from discord.ext import commands
import database
from config import DISCORD_TOKEN

# 1. 初始化資料庫
database.init_db()

# 2. 設定 Discord Bot
intents = discord.Intents.default()
intents.message_content = True
bot = commands.Bot(command_prefix='!', intents=intents)

@bot.event
async def setup_hook():
    await bot.load_extension("cogs.trading")
    try:
        synced = await bot.tree.sync()
        print(f"✅ 成功同步 {len(synced)} 個 Slash Commands")
    except Exception as e:
        print(f"❌ 同步指令失敗: {e}")

@bot.event
async def on_ready():
    print(f'🚀 Nexus Seeker 啟動成功！Bot ID: {bot.user}')
    print('等待美股排程觸發...')

if __name__ == "__main__":
    if not DISCORD_TOKEN:
        print("❌ 錯誤：找不到 DISCORD_TOKEN。")
    else:
        bot.run(DISCORD_TOKEN)