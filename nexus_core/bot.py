import discord
import logging
from discord.ext import commands
import asyncio
import database

logger = logging.getLogger(__name__)

class NexusBot(commands.Bot):
    def __init__(self):
        intents = discord.Intents.default()
        intents.message_content = True
        super().__init__(command_prefix='!', intents=intents)
        self.message_queue = asyncio.Queue()

    async def setup_hook(self):
        await self.load_extension("cogs.portfolio")
        await self.load_extension("cogs.watchlist")
        await self.load_extension("cogs.trading")
        await self.load_extension("cogs.research")
        await self.load_extension("cogs.debug")
        self.loop.create_task(self._message_worker())
        try:
            synced = await self.tree.sync()
            logger.info(f"✅ 成功同步 {len(synced)} 個 Slash Commands")
        except Exception as e:
            logger.error(f"❌ 同步指令失敗: {e}")

    async def on_ready(self):
        logger.info(f'初始化資料庫中...')
        database.init_db()
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

    async def _message_worker(self):
        """專職負責發送訊息的工人，確保系統不會因為發送訊息卡住"""
        await self.wait_until_ready()
        while not self.is_closed():
            # 1. 取得下一封要寄的信 (如果沒信會自動暫停在這裡，不耗效能)
            user_id, message, embed = await self.message_queue.get()
            
            try:
                user = await self.fetch_user(user_id)
                if user:
                    await user.send(content=message, embed=embed)
            except Exception as e:
                logger.error(f"發信失敗: {e}")
            
            # 2. 強制間隔 0.2 秒再寄下一封
            await asyncio.sleep(0.2)
            self.message_queue.task_done()
            
    async def queue_dm(self, user_id, message=None, embed=None):
        """將私訊排入背景佇列"""
        await self.message_queue.put((user_id, message, embed))

    async def notify_all_users(self, message):
        """一次將所有訊息排入背景寄發列隊"""
        user_ids = database.get_all_user_ids()
        count = 0
        for user_id in user_ids:
            await self.queue_dm(user_id, message=message)
            count += 1
        logger.info(f"已排程要發送通知給 {count} 位用戶: {message}")
