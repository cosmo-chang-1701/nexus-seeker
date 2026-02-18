import discord
import logging
from discord.ext import commands
import database

logger = logging.getLogger(__name__)

class NexusBot(commands.Bot):
    def __init__(self):
        intents = discord.Intents.default()
        intents.message_content = True
        super().__init__(command_prefix='!', intents=intents)

    async def setup_hook(self):
        await self.load_extension("cogs.trading")
        try:
            synced = await self.tree.sync()
            logger.info(f"✅ 成功同步 {len(synced)} 個 Slash Commands")
        except Exception as e:
            logger.error(f"❌ 同步指令失敗: {e}")

    async def on_ready(self):
        logger.info(f'🚀 Nexus Seeker 啟動成功！Bot ID: {self.user}')
        logger.info('等待美股排程觸發...')
        await self.notify_all_users("🚀 Nexus Seeker 機器人已啟動！")

    async def close(self):
        logger.info("🛑 Nexus Seeker 正在關閉...")
        try:
            await self.notify_all_users("🛑 Nexus Seeker 機器人正在關閉，請稍候...")
        except Exception as e:
            logger.error(f"發送關閉通知時發生錯誤: {e}")
        await super().close()

    async def notify_all_users(self, message):
        user_ids = database.get_all_user_ids()
        count = 0
        for user_id in user_ids:
            try:
                user = await self.fetch_user(user_id)
                if user:
                    await user.send(message)
                    count += 1
            except Exception as e:
                logger.warning(f"無法發送訊息給用戶 {user_id}: {e}")
        logger.info(f"已發送通知給 {count} 位用戶: {message}")
