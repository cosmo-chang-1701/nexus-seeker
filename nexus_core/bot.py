import discord
import logging
from discord.ext import commands
import asyncio
import os
import json
import uuid
from typing import Optional, Dict, Any, cast
import database
from database.leader_lock import (
    LOCK_NAME_DISCORD_BOT,
    try_acquire_leader_lock,
    release_leader_lock,
)

from database.notifications import (
    add_pending_notification,
    get_pending_notifications,
    delete_notification,
    get_pending_count,
)

logger = logging.getLogger(__name__)

DISCORD_CONTENT_LIMIT = 2000
DISCORD_EMBED_DESCRIPTION_LIMIT = 4000


def _split_plain_text(text: str, max_len: int) -> list[str]:
    """依邊界切分文字，盡量保留換行與空白結構。"""
    if len(text) <= max_len:
        return [text]

    chunks: list[str] = []
    start = 0
    while start < len(text):
        remaining = len(text) - start
        if remaining <= max_len:
            chunks.append(text[start:])
            break

        window = text[start : start + max_len]
        split_at: Optional[int] = None
        for separator in ("\n\n", "\n", " "):
            boundary = window.rfind(separator)
            if boundary >= int(max_len * 0.5):
                split_at = start + boundary + len(separator)
                break

        if split_at is None or split_at <= start:
            split_at = start + max_len

        chunks.append(text[start:split_at])
        start = split_at

    return chunks


def _split_discord_text(text: str, max_len: int) -> list[str]:
    """切分 Discord 訊息，必要時維持 code block 完整。"""
    if len(text) <= max_len:
        return [text]

    if text.startswith("```") and text.endswith("```"):
        first_newline = text.find("\n")
        if first_newline != -1:
            fence = text[:first_newline]
            body = text[first_newline + 1 : -3]
            reserved = len(fence) + len("\n") + len("\n```")
            body_room = max_len - reserved
            if body_room > 0:
                return [
                    f"{fence}\n{chunk}\n```"
                    for chunk in _split_plain_text(body, body_room)
                ]

    return _split_plain_text(text, max_len)


def _get_embed_length(embed: discord.Embed) -> int:
    """計算 Embed 的總字元數，用以評估是否超出 Discord 的 6000 字元限制。"""
    total = 0
    if embed.title:
        total += len(embed.title)
    if embed.description:
        total += len(embed.description)
    if embed.footer and embed.footer.text:
        total += len(embed.footer.text)
    if embed.author and embed.author.name:
        total += len(embed.author.name)
    for field in embed.fields:
        if field.name:
            total += len(field.name)
        if field.value:
            total += len(field.value)
    return total


class NexusBot(commands.Bot):
    def __init__(self):
        intents = discord.Intents.default()
        intents.message_content = True
        super().__init__(command_prefix="!", intents=intents)

        # Blue/Green-safe instance identity & leader lock state
        self.instance_id = os.getenv("NEXUS_INSTANCE_ID") or str(uuid.uuid4())
        self._is_leader_instance = False
        self._leader_lock_task: asyncio.Task | None = None
        self._leader_services_started = False

        # 仍然保留一個訊號訊號量，用於喚醒工人
        self.message_signal = asyncio.Event()
        self._has_notified_ready = False
        self._has_broadcast_startup_notice = False
        self._is_closing = False
        self._setup_done = False

    async def queue_dm(
        self, user_id: int, message: str = None, embed: discord.Embed = None
    ):
        """將私訊任務加入持久化佇列，並喚醒發送工人

        為了支援 Swarm start-first / 藍綠部署，本方法僅允許 leader instance 寫入列隊。
        """
        if not self._is_leader_instance:
            return
        from cogs.embed_builder import create_info_embed

        # 如果只有訊息且沒有 Embed，則將其封裝進 Embed
        if message and not embed:
            for chunk in _split_discord_text(message, DISCORD_EMBED_DESCRIPTION_LIMIT):
                chunk_embed = create_info_embed("Nexus Seeker 通知", chunk)
                await asyncio.to_thread(
                    add_pending_notification,
                    user_id,
                    None,
                    cast(Any, chunk_embed.to_dict()),
                )
            self.message_signal.set()
            return

        # 如果有 Embed 且超長，將其拆分後遞迴加入列隊
        if embed and _get_embed_length(embed) > 5500:
            from cogs.embed_builder import split_embed_by_fields

            split_embeds = split_embed_by_fields(embed)
            if len(split_embeds) > 1:
                for idx, s_embed in enumerate(split_embeds):
                    # 只有第一個 Split Embed 帶有原始訊息文字，避免重複發送
                    await self.queue_dm(
                        user_id,
                        message=message if idx == 0 else None,
                        embed=s_embed,
                    )
                return

        embed_dict: Optional[Dict[str, Any]] = (
            cast(Any, embed.to_dict()) if embed else None
        )
        if (
            embed
            and getattr(embed, "_view", None) is not None
            and embed_dict is not None
        ):
            embed_dict["_view"] = getattr(embed, "_view")
        # 1. 存入資料庫 (持久化)
        await asyncio.to_thread(add_pending_notification, user_id, message, embed_dict)
        # 2. 喚醒發送工人
        self.message_signal.set()

    async def setup_hook(self):
        if self._setup_done:
            return
        self._setup_done = True

        await self.load_extension("cogs.unified_terminal")
        await self.load_extension("cogs.terminal")
        await self.load_extension("cogs.trading")
        await self.load_extension("cogs.analyst_agent")
        await self.load_extension("cogs.intelligence")
        await self.load_extension("cogs.sentiment")
        await self.load_extension("cogs.hedging")
        await self.load_extension("cogs.calendar")
        await self.load_extension("cogs.order_ui")

        # 啟動背景任務與服務
        self.loop.create_task(self._message_worker())
        self.loop.create_task(self._health_worker())

        # 建立 leader-only 服務，但延後到 leader acquisition 才 start (blue/green 安全)
        try:
            from services.memory_manager import MemoryManager

            self.memory_manager = MemoryManager(self)
        except Exception as e:
            logger.error(f"❌ 建立記憶體管理員失敗: {e}")

        try:
            from services.hedge_monitor_service import HedgeMonitorService

            self.hedge_monitor = HedgeMonitorService(self)
        except Exception as e:
            logger.error(f"❌ 建立對沖監控服務失敗: {e}")

        try:
            from services.polymarket_service import PolymarketService

            self.polymarket_service = PolymarketService(self)
        except Exception as e:
            logger.error(f"❌ 建立 Polymarket 服務失敗: {e}")

        try:
            synced = await self.tree.sync()
            logger.info(f"✅ 成功同步 {len(synced)} 個 Slash Commands")
        except Exception as e:
            logger.error(f"❌ 同步指令失敗: {e}")

    async def on_ready(self):
        if self._has_notified_ready:
            logger.info("Bot 已重連，跳過啟動通知。")
            return

        self._has_notified_ready = True

        logger.info("初始化資料庫中...")
        try:
            await asyncio.to_thread(database.init_db)
            logger.info("✅ 資料庫初始化完成。")
        except Exception as e:
            logger.error(f"❌ 資料庫初始化失敗: {e}")

        # Acquire leader lock once immediately after migrations
        try:
            self._is_leader_instance = await asyncio.to_thread(
                try_acquire_leader_lock,
                LOCK_NAME_DISCORD_BOT,
                self.instance_id,
                int(os.getenv("NEXUS_LEADER_LOCK_TTL", "30")),
            )
        except Exception:
            self._is_leader_instance = False

        # Start leader lock loop after migrations (table must exist)
        if self._leader_lock_task is None:
            self._leader_lock_task = self.loop.create_task(self._leader_lock_loop())

        logger.info(
            f"🚀 Nexus Seeker 啟動成功！Bot ID: {self.user} | instance_id={self.instance_id} | leader={self._is_leader_instance}"
        )

        if self._is_leader_instance:
            self._start_leader_services()

        # 啟動後檢查有無遺留通知並喚醒工人 (leader only)
        if self._is_leader_instance:
            try:
                pending_count = await asyncio.to_thread(get_pending_count)
                if pending_count > 0:
                    logger.info(
                        f"發現 {pending_count} 條遺留的待發送通知，啟動補發流程..."
                    )
                    self.message_signal.set()
            except Exception as e:
                logger.error(f"檢查待發送通知時出錯: {e}")

            # 核心改進：將啟動通知改為背景任務，避免阻塞 on_ready
            logger.info("正在準備背景啟動通知...")
            self._has_broadcast_startup_notice = True
            asyncio.create_task(self.notify_all_users("🚀 Nexus Seeker 機器人已啟動！"))

        logger.info("✅ on_ready 流程處理完畢，機器人進入運行狀態。")

    async def close(self):
        if self._is_closing:
            return
        self._is_closing = True

        logger.info("🛑 Nexus Seeker 正在關閉...")

        # Stop leader lock loop and release lease
        if self._leader_lock_task is not None:
            self._leader_lock_task.cancel()
            self._leader_lock_task = None
        try:
            await asyncio.to_thread(
                release_leader_lock, LOCK_NAME_DISCORD_BOT, self.instance_id
            )
        except Exception:
            pass

        # 停止 leader-only 服務 (即便尚未啟動也安全)
        self._stop_leader_services()

        # 發送關閉通知 (leader only)
        if self._is_leader_instance:
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

    def _start_leader_services(self) -> None:
        if self._leader_services_started:
            return
        self._leader_services_started = True

        try:
            if hasattr(self, "memory_manager"):
                self.memory_manager.start()
        except Exception as e:
            logger.error(f"❌ 啟動記憶體管理員失敗: {e}")

        try:
            if hasattr(self, "hedge_monitor"):
                self.hedge_monitor.start()
        except Exception as e:
            logger.error(f"❌ 啟動對沖監控服務失敗: {e}")

        try:
            if hasattr(self, "polymarket_service"):
                self.polymarket_service.start()
        except Exception as e:
            logger.error(f"❌ 啟動 Polymarket 服務失敗: {e}")

    def _stop_leader_services(self) -> None:
        if not self._leader_services_started:
            return
        self._leader_services_started = False

        try:
            if hasattr(self, "memory_manager"):
                self.memory_manager.stop()
        except Exception:
            pass

        try:
            if hasattr(self, "hedge_monitor"):
                self.hedge_monitor.stop()
        except Exception:
            pass

        try:
            if hasattr(self, "polymarket_service"):
                self.polymarket_service.stop()
        except Exception:
            pass

    async def _leader_lock_loop(self):
        """Maintain a SQLite leader lease to support Swarm start-first blue/green deploy."""
        ttl = int(os.getenv("NEXUS_LEADER_LOCK_TTL", "30"))
        interval = int(os.getenv("NEXUS_LEADER_LOCK_INTERVAL", "10"))

        await self.wait_until_ready()

        while not self.is_closed():
            try:
                acquired = await asyncio.to_thread(
                    try_acquire_leader_lock,
                    LOCK_NAME_DISCORD_BOT,
                    self.instance_id,
                    ttl,
                )

                if acquired != self._is_leader_instance:
                    self._is_leader_instance = acquired
                    if acquired:
                        logger.warning(
                            f"🟢 Leader acquired: instance_id={self.instance_id} (blue/green promote)"
                        )
                        self._start_leader_services()
                        self.message_signal.set()

                        # follower → leader：補發啟動通知（每個實例生命週期僅一次）
                        if (
                            not self._is_closing
                            and not self._has_broadcast_startup_notice
                        ):
                            self._has_broadcast_startup_notice = True
                            asyncio.create_task(
                                self.notify_all_users("🚀 Nexus Seeker 機器人已啟動！")
                            )
                    else:
                        logger.warning(
                            f"🔴 Leader lost: instance_id={self.instance_id} (blue/green demote)"
                        )
                        self._stop_leader_services()
            except Exception as e:
                logger.debug(f"Leader lock loop error: {e}")

            await asyncio.sleep(interval)

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
            # leader-only: avoid double-send during blue/green overlap
            if not self._is_leader_instance:
                await asyncio.sleep(2)
                continue

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

                view_info = embed_dict.pop("_view", None) if embed_dict else None
                embed = discord.Embed.from_dict(embed_dict) if embed_dict else None
                view = None
                if view_info:
                    if view_info.startswith("ApplyTelemetryView:"):
                        sug_str = view_info.split(":", 1)[1]
                        try:
                            from cogs.order_ui import ApplyTelemetryView

                            sug_raw = json.loads(sug_str)
                            suggestions = {
                                int(k): (float(v[0]), int(v[1]))
                                for k, v in sug_raw.items()
                            }
                            view = ApplyTelemetryView(suggestions)
                        except Exception as e:
                            logger.error(f"Failed to rebuild ApplyTelemetryView: {e}")

                try:
                    user = await self.fetch_user(user_id)
                    if user:
                        message_chunks = (
                            _split_discord_text(message, DISCORD_CONTENT_LIMIT)
                            if message
                            else [None]
                        )

                        for index, chunk in enumerate(message_chunks):
                            await user.send(
                                content=chunk or None,
                                embed=embed if index == 0 else None,
                                view=view if (index == 0 and view) else None,
                            )
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
                    if e.status == 400:
                        logger.error(
                            f"永久發送錯誤(HTTP 400 Bad Request)，已將該通知從列隊刪除以防阻塞: uid={user_id}"
                        )
                        await asyncio.to_thread(delete_notification, notif_id)
                    else:
                        # 429 或 5xx 可能需要重試，這裡簡單間隔後繼續
                        await asyncio.sleep(2)
                except Exception as e:
                    logger.error(f"發信失敗(Unexpected): uid={user_id}, err={e}")

                # 間隔 0.2 秒再寄下一封，避免觸發速率限制
                await asyncio.sleep(0.2)

    async def notify_all_users(self, message):
        """一次將所有訊息排入背景寄發列隊 (優化為非阻塞)"""
        if not self._is_leader_instance:
            return
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
