import discord
import logging
from discord.ext import commands
import database
from config import DISCORD_TOKEN, LOG_LEVEL

# 0. 設定日誌
logging.basicConfig(level=getattr(logging, LOG_LEVEL), format='%(asctime)s [%(levelname)s] %(name)s: %(message)s')
logger = logging.getLogger(__name__)

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
        logger.info(f"✅ 成功同步 {len(synced)} 個 Slash Commands")
    except Exception as e:
        logger.error(f"❌ 同步指令失敗: {e}")

@bot.event
async def on_ready():
    logger.info(f'🚀 Nexus Seeker 啟動成功！Bot ID: {bot.user}')
    logger.info('等待美股排程觸發...')

if __name__ == "__main__":
    if not DISCORD_TOKEN:
        logger.error("❌ 錯誤：找不到 DISCORD_TOKEN。")
    else:
        bot.run(DISCORD_TOKEN)