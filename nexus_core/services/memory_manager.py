import asyncio
import psutil
import logging
import gc
import os
import discord
from datetime import datetime, timezone

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
        self._warmup_task = None
        self._check_interval = 300  # 5 分鐘檢查一次
        self._last_alert_at = 0
        self._last_warmup_date = None

    def start(self):
        if self.running:
            return
        self.running = True
        self._monitor_task = asyncio.create_task(self._monitor_loop())
        self._warmup_task = asyncio.create_task(self._warmup_loop())
        logger.info("🧠 Memory Manager Service started.")

    def stop(self):
        self.running = False
        if self._monitor_task:
            self._monitor_task.cancel()
        if self._warmup_task:
            self._warmup_task.cancel()
        logger.info("🛑 Memory Manager Service stopped.")

    async def _monitor_loop(self):
        while self.running:
            try:
                await self._perform_health_check()
            except Exception as e:
                logger.error(f"Health check error: {e}")
            await asyncio.sleep(self._check_interval)

    async def _warmup_loop(self):
        """🚀 Task 2: 定期檢查盤前預熱視窗 (08:30 - 09:30 ET)"""
        while self.running:
            try:
                from market_time import ny_tz

                now_ny = datetime.now(ny_tz)
                # 08:30 - 09:30 ET 視窗
                if 8 <= now_ny.hour <= 9:
                    if now_ny.hour == 8 and now_ny.minute < 30:
                        pass
                    elif now_ny.hour == 9 and now_ny.minute > 30:
                        pass
                    else:
                        await self.proactive_warmup()
            except Exception as e:
                logger.error(f"Warmup loop error: {e}")
            await asyncio.sleep(600)  # 每 10 分鐘檢查一次

    async def proactive_warmup(self):
        """執行快取預熱，具備冪等性與記憶體保護門檻。"""
        today_str = datetime.now().strftime("%Y-%m-%d")
        if self._last_warmup_date == today_str:
            return

        mem = psutil.virtual_memory()
        if mem.percent > 85.0:
            logger.warning(
                f"🚨 [Warmup Gate] RAM usage ({mem.percent}%) too high, skipping cache warmup."
            )
            return

        logger.info("🔥 [Warmup] 啟動盤前快取預熱 (Cache Warmup)...")
        try:
            from database.watchlist import get_all_watchlist
            from services.market_data_service import get_sma, get_ema, get_quote

            watchlist = get_all_watchlist()
            symbols = list(set([row[1] for row in watchlist]))
            # 確保 SPY 優先預熱
            if "SPY" not in symbols:
                symbols.insert(0, "SPY")
            else:
                symbols.remove("SPY")
                symbols.insert(0, "SPY")

            for sym in symbols[:20]:  # 限制數量以防 OOM
                # 平行預熱常用指標
                await asyncio.gather(
                    get_quote(sym),
                    get_sma(sym, 200),
                    get_ema(sym, 8),
                    get_ema(sym, 21),
                    return_exceptions=True,
                )
                # 每個標的間隔一下，避免 CPU 瞬間飆升
                await asyncio.sleep(0.5)

            self._last_warmup_date = today_str
            logger.info(
                f"✅ [Warmup] 快取預熱完成。共處理 {len(symbols[:20])} 檔標的。"
            )
        except Exception as e:
            logger.error(f"Cache warmup failed: {e}")

    async def _perform_health_check(self):
        mem = psutil.virtual_memory()
        process = psutil.Process(os.getpid())
        proc_mem = process.memory_info().rss / (1024 * 1024)

        # 1. 定期垃圾回收 (基本維護)
        if mem.percent > 80:
            gc.collect()
            logger.info(
                f"🧹 [記憶體維護] 檢測到 RAM 使用率為 {mem.percent}%，已手動觸發 GC。"
            )

        # 2. 觸發警報
        if mem.percent > self.threshold:
            now = datetime.now(timezone.utc).timestamp()
            # 限制警報頻率 (1 小時一次)
            if now - self._last_alert_at > 3600:
                await self._trigger_emergency_alert(mem.percent, proc_mem)
                self._last_alert_at = now

    async def _trigger_emergency_alert(self, total_usage: float, proc_mem: float):
        from config import DISCORD_ADMIN_USER_ID

        if not DISCORD_ADMIN_USER_ID:
            return

        embed = discord.Embed(
            title="🆘 【系統緊急警報：記憶體不足】",
            description=f"VPS 記憶體使用量已達臨界值 (`{total_usage}%`)，可能導致程序被 OOM Killer 終止。",
            color=discord.Color.red(),
            timestamp=discord.utils.utcnow(),
        )

        embed.add_field(name="當前總占用", value=f"`{total_usage}%`", inline=True)
        embed.add_field(
            name="程序占用 (RSS)", value=f"`{proc_mem:.1f} MB`", inline=True
        )

        # 嘗試列出最大的快取對象
        from services import market_data_service

        sma_count = len(market_data_service._sma_cache)
        ema_count = len(market_data_service._ema_cache)

        embed.add_field(
            name="📦 快取消費者",
            value=f"SMA/EMA: `{sma_count}/{ema_count}` 筆",
            inline=False,
        )
        embed.set_footer(text="建議重啟服務或增加 Swap 分區。")

        await self.bot.queue_dm(DISCORD_ADMIN_USER_ID, embed=embed)
        logger.warning(f"🚨 [OOM 警報] 記憶體使用率過高: {total_usage}%")
