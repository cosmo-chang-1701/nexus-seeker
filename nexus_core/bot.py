import discord
import logging
from discord.ext import commands
import asyncio
import database
from database.notifications import (
    add_pending_notification,
    get_pending_notifications,
    delete_notification,
    get_pending_count,
)

logger = logging.getLogger(__name__)


class NexusBot(commands.Bot):
    def __init__(self):
        intents = discord.Intents.default()
        intents.message_content = True
        super().__init__(command_prefix="!", intents=intents)
        # 仍然保留一個訊號訊號量，用於喚醒工人
        self.message_signal = asyncio.Event()
        self._has_notified_ready = False
        self._is_closing = False

    async def queue_dm(
        self, user_id: int, message: str = None, embed: discord.Embed = None
    ):
        """將私訊任務加入持久化佇列，並喚醒發送工人"""
        embed_dict = embed.to_dict() if embed else None
        # 1. 存入資料庫 (持久化)
        await asyncio.to_thread(add_pending_notification, user_id, message, embed_dict)
        # 2. 喚醒發送工人
        self.message_signal.set()

    async def setup_hook(self):
        await self.load_extension("cogs.unified_terminal")
        await self.load_extension("cogs.terminal")
        await self.load_extension("cogs.trading")
        await self.load_extension("cogs.analyst_agent")
        await self.load_extension("cogs.intelligence")
        await self.load_extension("cogs.sentiment")
        await self.load_extension("cogs.hedging")
        await self.load_extension("cogs.calendar")

        # 啟動背景任務與服務
        self.loop.create_task(self._message_worker())
        self.loop.create_task(self._health_worker())

        # 啟動記憶體管理員 (1GB RAM 優化)
        try:
            from services.memory_manager import MemoryManager

            self.memory_manager = MemoryManager(self)
            self.memory_manager.start()
        except Exception as e:
            logger.error(f"❌ 啟動記憶體管理員失敗: {e}")

        # 啟動對沖監控服務
        try:
            from services.hedge_monitor_service import HedgeMonitorService

            self.hedge_monitor = HedgeMonitorService(self)
            self.hedge_monitor.start()
        except Exception as e:
            logger.error(f"❌ 啟動對沖監控服務失敗: {e}")

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
        if self._has_notified_ready:
            logger.info("Bot 已重連，跳過啟動通知。")
            return

        logger.info("初始化資料庫中...")
        try:
            await asyncio.to_thread(database.init_db)
            logger.info("✅ 資料庫初始化完成。")
        except Exception as e:
            logger.error(f"❌ 資料庫初始化失敗: {e}")

        logger.info(f"🚀 Nexus Seeker 啟動成功！Bot ID: {self.user}")

        # 啟動後檢查有無遺留通知並喚醒工人
        try:
            pending_count = await asyncio.to_thread(get_pending_count)
            if pending_count > 0:
                logger.info(f"發現 {pending_count} 條遺留的待發送通知，啟動補發流程...")
                self.message_signal.set()
        except Exception as e:
            logger.error(f"檢查待發送通知時出錯: {e}")

        # 核心改進：將啟動通知改為背景任務，避免阻塞 on_ready
        logger.info("正在準備背景啟動通知...")
        asyncio.create_task(self.notify_all_users("🚀 Nexus Seeker 機器人已啟動！"))

        self._has_notified_ready = True
        logger.info("✅ on_ready 流程處理完畢，機器人進入運行狀態。")

    async def close(self):
        if self._is_closing:
            return
        self._is_closing = True

        logger.info("🛑 Nexus Seeker 正在關閉...")

        # 停止記憶體管理員
        if hasattr(self, "memory_manager"):
            try:
                self.memory_manager.stop()
            except Exception as e:
                logger.error(f"停止記憶體管理員時出錯: {e}")

        # 停止對沖監控服務
        if hasattr(self, "hedge_monitor"):
            try:
                self.hedge_monitor.stop()
            except Exception as e:
                logger.error(f"停止對沖監控服務時出錯: {e}")

        # 停止 Polymarket 服務
        if hasattr(self, "polymarket_service"):
            try:
                self.polymarket_service.stop()
            except Exception as e:
                logger.error(f"停止 Polymarket 服務時出錯: {e}")

        # 發送關閉通知
        try:
            await self.notify_all_users("🛑 Nexus Seeker 機器人正在關閉，請稍候...")
        except Exception as e:
            logger.error(f"發送關閉通知時發生錯誤: {e}")

        # ⏳ 核心改進：等待所有持久化訊息送出 (或直到 Docker 強制終止)
        wait_time = 0
        while await asyncio.to_thread(get_pending_count) > 0 and wait_time < 30:
            if wait_time % 5 == 0:
                logger.info(
                    f"正在等待訊息佇列清空 (剩餘 {await asyncio.to_thread(get_pending_count)} 條)..."
                )
            await asyncio.sleep(1)
            wait_time += 1

        await super().close()

    async def _health_worker(self):
        """定期更新健康狀態檔案，讓 Docker 能夠識別機器人的健康度。"""
        import time

        # 啟動時立即寫入一次，確保 Docker Healthcheck 不會太快判定失敗
        try:
            with open("/tmp/bot_healthy", "w") as f:
                f.write(str(time.time()))
        except Exception as e:
            logger.error(f"初始寫入 bot_healthy 失敗: {e}")

        await self.wait_until_ready()

        while not self.is_closed():
            try:
                # 寫入 /tmp 資料夾以更新時間戳記
                with open("/tmp/bot_healthy", "w") as f:
                    f.write(str(time.time()))
            except Exception as e:
                logger.error(f"寫入 bot_healthy 檔案失敗: {e}")
            await asyncio.sleep(60)

    async def _message_worker(self):
        """專職負責發送訊息的工人，從資料庫讀取待發送清單"""
        await self.wait_until_ready()

        while not self.is_closed():
            # 1. 取得下一批待發送通知
            pending = await asyncio.to_thread(get_pending_notifications, limit=10)

            if not pending:
                # 如果沒信，進入等待狀態
                self.message_signal.clear()
                try:
                    await asyncio.wait_for(self.message_signal.wait(), timeout=60)
                except asyncio.TimeoutError:
                    pass
                continue

            # 2. 逐一處理通知
            for notif_id, user_id, message, embed_dict in pending:
                if self.is_closed():
                    break

                embed = discord.Embed.from_dict(embed_dict) if embed_dict else None

                try:
                    user = await self.fetch_user(user_id)
                    if user:
                        await user.send(content=message, embed=embed)
                        # 發送成功才從資料庫刪除
                        await asyncio.to_thread(delete_notification, notif_id)
                except discord.Forbidden as e:
                    logger.warning(f"發信失敗(Forbidden): uid={user_id}, err={e}")
                    await asyncio.to_thread(
                        delete_notification, notif_id
                    )  # 無權限直接放棄
                except discord.NotFound as e:
                    logger.warning(f"發信失敗(NotFound): uid={user_id}, err={e}")
                    await asyncio.to_thread(delete_notification, notif_id)
                except discord.HTTPException as e:
                    logger.error(
                        f"發信失敗(HTTPException): uid={user_id}, status={e.status}, err={e}"
                    )
                    # 429 或 5xx 可能需要重試，這裡簡單間隔後繼續
                    await asyncio.sleep(2)
                except Exception as e:
                    logger.error(f"發信失敗(Unexpected): uid={user_id}, err={e}")

                # 間隔 0.2 秒再寄下一封，避免觸發速率限制
                await asyncio.sleep(0.2)

    async def notify_all_users(self, message):
        """一次將所有訊息排入背景寄發列隊 (優化為非阻塞)"""
        try:
            from database.user_settings import get_all_user_ids

            user_ids = await asyncio.to_thread(get_all_user_ids)

            count = 0
            for user_id in user_ids:
                await self.queue_dm(user_id, message=message)
                count += 1
            logger.info(f"已將啟動通知排入 {count} 位用戶的發送列隊。")
        except Exception as e:
            logger.error(f"通知所有用戶時出錯: {e}")
