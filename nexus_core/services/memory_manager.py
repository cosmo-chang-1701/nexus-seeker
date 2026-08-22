from typing import Any
import asyncio
import psutil
import logging
import gc
import os
from datetime import datetime, timezone

from cogs.embed_builder import create_memory_alert_embed
from services.llm_service import is_memory_safe

logger = logging.getLogger(__name__)


class MemoryManager:
    """
    系統記憶體管理員：負責監控 VPS 資源、執行垃圾回收與觸發緊急警報。
    專為 1GB RAM 環境優化。
    """

    def __init__(self, bot: Any, threshold: float = 90.0):
        self.bot = bot
        self.threshold = threshold
        self.running = False
        self._monitor_task = None
        self._warmup_task = None
        self._check_interval = 300  # 5 分鐘檢查一次
        self._last_alert_at = 0
        self._last_power_alert_level = 100
        self._last_warmup_date = None

    def start(self) -> None:
        if self.running:
            return
        self.running = True
        self._monitor_task = asyncio.create_task(self._monitor_loop())  # type: ignore
        self._warmup_task = asyncio.create_task(self._warmup_loop())  # type: ignore
        logger.info("🧠 Memory Manager Service started.")

    def stop(self) -> None:
        self.running = False
        if self._monitor_task:
            self._monitor_task.cancel()
        if self._warmup_task:
            self._warmup_task.cancel()
        logger.info("🛑 Memory Manager Service stopped.")

    async def _monitor_loop(self) -> None:
        while self.running:
            try:
                await self._perform_health_check()
            except Exception as e:
                logger.error(f"Health check error: {e}")
            await asyncio.sleep(self._check_interval)

    async def _warmup_loop(self) -> None:
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

    async def proactive_warmup(self) -> None:
        """執行快取預熱，具備冪等性與記憶體保護門檻。"""
        today_str = datetime.now().strftime("%Y-%m-%d")
        if self._last_warmup_date == today_str:
            return

        if not is_memory_safe():
            logger.warning(
                "🚨 [Warmup Gate] Resource usage too high (RAM+Swap >= 85%), skipping cache warmup."
            )
            return

        logger.info("🔥 [Warmup] 啟動盤前快取預熱 (Cache Warmup)...")
        try:
            from database.watchlist import get_all_watchlist
            from services.market_data_service import get_sma, get_ema, get_quote

            watchlist = get_all_watchlist()
            symbols: list[str] = list(set([row[1] for row in watchlist]))
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

            self._last_warmup_date = today_str  # type: ignore
            logger.info(
                f"✅ [Warmup] 快取預熱完成。共處理 {len(symbols[:20])} 檔標的。"
            )
        except Exception as e:
            logger.error(f"Cache warmup failed: {e}")

    async def _perform_health_check(self) -> None:
        mem = psutil.virtual_memory()
        swap = psutil.swap_memory()
        process = psutil.Process(os.getpid())
        proc_mem = process.memory_info().rss / (1024 * 1024)

        # 1. 定期垃圾回收 (基本維護)
        if mem.percent > 80 or swap.percent > 40:
            gc.collect()
            logger.info(
                f"🧹 [記憶體維護] 檢測到 RAM 使用率為 {mem.percent}% (Swap: {swap.percent}%)，已手動觸發 GC。"
            )

        # 2. 觸發主節點警報
        now = datetime.now(timezone.utc).timestamp()
        if mem.percent > self.threshold or swap.percent > 80:
            # 限制警報頻率 (1 小時一次)
            if now - self._last_alert_at > 3600:
                await self._trigger_emergency_alert(
                    mem.percent, proc_mem, swap.percent, source="Droplet (主節點)"
                )
                self._last_alert_at = now  # type: ignore

        # 3. 觸發邊緣節點警報
        import config

        tunnel_url = getattr(config, "TUNNEL_URL", "")
        if tunnel_url:
            import httpx

            try:
                async with httpx.AsyncClient(timeout=3.0) as client:
                    res = await client.get(
                        f"{tunnel_url.rstrip('/')}/api/v1/health/sys"
                    )
                    if res.status_code == 200:
                        edge = res.json()
                        edge_mem_pct = edge.get("memory_percent", 0)
                        edge_swap_pct = edge.get("swap_percent", 0)
                        if edge_mem_pct > self.threshold or edge_swap_pct > 80:
                            if now - self._last_alert_at > 3600:
                                edge_os = edge.get("os_system", "Edge")
                                await self._trigger_emergency_alert(
                                    edge_mem_pct,
                                    edge.get("process_memory_mb", 0),
                                    edge_swap_pct,
                                    source=f"{edge_os} (邊緣節點)",
                                )
                                self._last_alert_at = now  # type: ignore

                        # Check battery (0%, 25%, 50%, 75% thresholds)
                        battery = edge.get("battery")
                        if battery:
                            if not battery.get("power_plugged"):
                                percent = battery.get("percent", 100)
                                alert_level = None
                                last_level = getattr(
                                    self, "_last_power_alert_level", 100
                                )

                                if (
                                    percent <= 5 and last_level > 0
                                ):  # 0% is usually dead, alert at <= 5% for the 0% level
                                    alert_level = 0
                                elif percent <= 25 and last_level > 25:
                                    alert_level = 25
                                elif percent <= 50 and last_level > 50:
                                    alert_level = 50
                                elif percent <= 75 and last_level > 75:
                                    alert_level = 75

                                if alert_level is not None:
                                    from cogs.embed_builder import (
                                        create_power_alert_embed,
                                    )
                                    from config import DISCORD_ADMIN_USER_ID

                                    if DISCORD_ADMIN_USER_ID:
                                        edge_os = edge.get("os_system", "Edge")
                                        embed = create_power_alert_embed(
                                            percent=percent,
                                            secsleft=battery.get("secsleft", -1),
                                            source=f"{edge_os} (邊緣節點)",
                                        )
                                        await self.bot.queue_dm(
                                            DISCORD_ADMIN_USER_ID, embed=embed
                                        )
                                        logger.warning(
                                            f"🚨 [電力警報 - {edge_os}] 邊緣節點電量下降至 {percent}% (警報層級: {alert_level}%)"
                                        )
                                    self._last_power_alert_level = alert_level
                            else:
                                # Reset tracking when plugged in
                                self._last_power_alert_level = 100
            except Exception:
                pass  # Edge offline, ignore for background alerts

    async def _trigger_emergency_alert(
        self,
        total_usage: float,
        proc_mem: float,
        swap_usage: float = 0.0,
        source: str = "Droplet (主節點)",
    ) -> Any:
        from config import DISCORD_ADMIN_USER_ID

        if not DISCORD_ADMIN_USER_ID:
            return

        # 嘗試列出最大的快取對象
        from services import market_data_service

        sma_count = len(market_data_service._sma_cache)
        ema_count = len(market_data_service._ema_cache)

        embed = create_memory_alert_embed(
            total_usage=total_usage,
            process_memory_mb=proc_mem,
            sma_cache_size=sma_count,
            ema_cache_size=ema_count,
            swap_usage=swap_usage,
            source=source,
        )

        await self.bot.queue_dm(DISCORD_ADMIN_USER_ID, embed=embed)
        logger.warning(
            f"🚨 [OOM 警報 - {source}] 記憶體使用率過高 (RAM: {total_usage}%, Swap: {swap_usage}%)"
        )
