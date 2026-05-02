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
        await self.load_extension("cogs.analyst_agent")
        
        # 啟動背景任務與服務
        self.loop.create_task(self._message_worker())
        self.loop.create_task(self._health_worker())
        
        # 啟動 Polymarket 巨鯨監控服務
        try:
            from services.polymarket_service import PolymarketService
            self.polymarket_service = PolymarketService(self)
            self.polymarket_service.start()
        except Exception as e:
            logger.error(f"❌ 啟動 Polymarket 服務失敗: {e}")

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
        
        # 停止 Polymarket 服務
        if hasattr(self, 'polymarket_service'):
            try:
                self.polymarket_service.stop()
            except Exception as e:
                logger.error(f"停止 Polymarket 服務時出錯: {e}")

        try:
            await self.notify_all_users("🛑 Nexus Seeker 機器人正在關閉，請稍候...")
        except Exception as e:
            logger.error(f"發送關閉通知時發生錯誤: {e}")
        await super().close()

    async def _health_worker(self):
        """定期更新健康狀態檔案，讓 Docker 能夠識別機器人的健康度。"""
        await self.wait_until_ready()
        import time
        while not self.is_closed():
            try:
                # 寫入 /tmp 資料夾以更新時間戳記
                with open("/tmp/bot_healthy", "w") as f:
                    f.write(str(time.time()))
            except Exception as e:
                logger.error(f"寫入 bot_healthy 檔案失敗: {e}")
            await asyncio.sleep(60)

    async def _message_worker(self):
        """專職負責發送訊息的工人，確保系統不會因為發送訊息卡住"""
        await self.wait_until_ready()
        while not self.is_closed():
            # 1. 取得下一封要寄的信 (如果沒信會自動暫停在這裡，不耗效能)
            user_id, message, embed = await self.message_queue.get()
            has_embed = embed is not None
            field_count = len(embed.fields) if has_embed else 0
            
            try:
                user = await self.fetch_user(user_id)
                if user:
                    await user.send(content=message, embed=embed)
            except discord.Forbidden as e:
                logger.warning(f"發信失敗(Forbidden): uid={user_id}, has_embed={has_embed}, fields={field_count}, err={e}")
            except discord.NotFound as e:
                logger.warning(f"發信失敗(NotFound): uid={user_id}, has_embed={has_embed}, fields={field_count}, err={e}")
            except discord.HTTPException as e:
                logger.error(f"發信失敗(HTTPException): uid={user_id}, has_embed={has_embed}, fields={field_count}, status={e.status}, err={e}")
            except Exception as e:
                logger.error(f"發信失敗(Unexpected): uid={user_id}, has_embed={has_embed}, fields={field_count}, err={e}")
            
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
