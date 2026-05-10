import asyncio
import psutil
import logging
import gc
import os
import discord
from datetime import datetime, timezone
from typing import Dict, Any

logger = logging.getLogger(__name__)

class MemoryManager:
    """
    系統記憶體管理員：負責監控 VPS 資源、執行垃圾回收與觸發緊急警報。
    專為 1GB RAM 環境優化。
    """
    def __init__(self, bot, threshold: float = 90.0):
        self.bot = bot
        self.threshold = threshold
        self.running = False
        self._monitor_task = None
        self._check_interval = 300 # 5 分鐘檢查一次
        self._last_alert_at = 0

    def start(self):
        if self.running: return
        self.running = True
        self._monitor_task = asyncio.create_task(self._monitor_loop())
        logger.info("🧠 Memory Manager Service started.")

    def stop(self):
        self.running = False
        if self._monitor_task:
            self._monitor_task.cancel()
        logger.info("🛑 Memory Manager Service stopped.")

    async def _monitor_loop(self):
        while self.running:
            try:
                await self._perform_health_check()
            except Exception as e:
                logger.error(f"Health check error: {e}")
            await asyncio.sleep(self._check_interval)

    async def _perform_health_check(self):
        mem = psutil.virtual_memory()
        process = psutil.Process(os.getpid())
        proc_mem = process.memory_info().rss / (1024 * 1024)
        
        # 1. 定期垃圾回收 (基本維護)
        if mem.percent > 80:
            gc.collect()
            logger.info(f"🧹 [記憶體維護] 檢測到 RAM 使用率為 {mem.percent}%，已手動觸發 GC。")

        # 2. 觸發警報
        if mem.percent > self.threshold:
            now = datetime.now(timezone.utc).timestamp()
            # 限制警報頻率 (1 小時一次)
            if now - self._last_alert_at > 3600:
                await self._trigger_emergency_alert(mem.percent, proc_mem)
                self._last_alert_at = now

    async def _trigger_emergency_alert(self, total_usage: float, proc_mem: float):
        from config import DISCORD_ADMIN_USER_ID
        
        if not DISCORD_ADMIN_USER_ID: return

        embed = discord.Embed(
            title="🆘 【系統緊急警報：記憶體不足】",
            description=f"VPS 記憶體使用量已達臨界值 (`{total_usage}%`)，可能導致程序被 OOM Killer 終止。",
            color=discord.Color.red(),
            timestamp=discord.utils.utcnow()
        )
        
        embed.add_field(name="當前總占用", value=f"`{total_usage}%`", inline=True)
        embed.add_field(name="程序占用 (RSS)", value=f"`{proc_mem:.1f} MB`", inline=True)
        
        # 嘗試列出最大的快取對象
        from services import market_data_service
        sma_count = len(market_data_service._sma_cache)
        ema_count = len(market_data_service._ema_cache)
        
        embed.add_field(name="📦 快取消費者", value=f"SMA/EMA: `{sma_count}/{ema_count}` 筆", inline=False)
        embed.set_footer(text="建議重啟服務或增加 Swap 分區。")
        
        await self.bot.queue_dm(DISCORD_ADMIN_USER_ID, embed=embed)
        logger.warning(f"🚨 [OOM 警報] 記憶體使用率過高: {total_usage}%")
