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

    def __init__(
        self,
        bot: Any,
        threshold: float | None = None,
        swap_threshold: float | None = None,
        swap_critical: float | None = None,
    ) -> None:
        import config

        self.bot = bot
        self.threshold: float = (
            float(threshold)
            if threshold is not None
            else getattr(config, "MEMORY_ALERT_THRESHOLD", 90.0)
        )
        self.swap_threshold: float = (
            float(swap_threshold)
            if swap_threshold is not None
            else getattr(config, "MEMORY_SWAP_ALERT_THRESHOLD", 50.0)
        )
        self.swap_critical: float = (
            float(swap_critical)
            if swap_critical is not None
            else getattr(config, "MEMORY_SWAP_CRITICAL_THRESHOLD", 80.0)
        )
        self.running = False
        self._monitor_task = None
        self._warmup_task = None
        self._check_interval = 300  # 5 分鐘檢查一次
        self._last_alert_at: float = 0.0
        self._last_alerts: dict[str, float] = {}
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

    def is_memory_critical(
        self,
        mem_percent: float,
        swap_percent: float,
        swap_total: float | None = None,
    ) -> bool:
        """
        綜合判定實體 RAM 與 Swap 是否達到緊急警報條件。

        判定標準：
        1. 臨界 Swap 耗盡 (swap_percent >= self.swap_critical，預設 80%)：
           代表虛擬置換空間將盡，系統極可能面臨 Page Thrashing 導致 OOM。
        2. 系統確認無 Swap 分區環境 (swap_total is not None 且 swap_total <= 0)：
           此時無任何置換緩衝，RAM 達到門檻 (mem_percent >= self.threshold，預設 90%)
           即構成直接 OOM 威脅。
        3. 正常配置 Swap 環境 (或 swap_total 未知)：
           單純 RAM 偏高不觸發警報（避免 Linux 正常的 buffer/cache 行為造成誤報），
           必須同時滿足 RAM 飽和 (mem_percent >= self.threshold) 且
           Swap 顯著受壓 (swap_percent >= self.swap_threshold，預設 50%)。
        """
        # 1. Swap 瀕臨耗盡
        if swap_percent >= self.swap_critical:
            return True

        # 2. 系統無 Swap 分區保護
        if swap_total is not None and swap_total <= 0:
            return bool(mem_percent >= self.threshold)

        # 3. 綜合判定：RAM 飽和且 Swap 達到顯著壓力門檻
        return bool(
            mem_percent >= self.threshold and swap_percent >= self.swap_threshold
        )

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
        source_main = "Droplet (主節點)"
        if self.is_memory_critical(
            mem.percent, swap.percent, swap_total=float(swap.total)
        ):
            # 限制警報頻率 (1 小時一次)
            if now - self._last_alerts.get(source_main, 0.0) > 3600:
                await self._trigger_emergency_alert(
                    mem.percent, proc_mem, swap.percent, source=source_main
                )
                self._last_alerts[source_main] = now
                self._last_alert_at = now

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
                        edge_mem_pct = float(edge.get("memory_percent", 0.0))
                        edge_swap_pct = float(edge.get("swap_percent", 0.0))
                        edge_swap_total_raw = edge.get("swap_total_mb")
                        edge_swap_total = (
                            float(edge_swap_total_raw)
                            if edge_swap_total_raw is not None
                            else None
                        )
                        edge_os = edge.get("os_system", "Edge")
                        source_edge = f"{edge_os} (邊緣節點)"
                        if self.is_memory_critical(
                            edge_mem_pct, edge_swap_pct, swap_total=edge_swap_total
                        ):
                            if now - self._last_alerts.get(source_edge, 0.0) > 3600:
                                await self._trigger_emergency_alert(
                                    edge_mem_pct,
                                    edge.get("process_memory_mb", 0.0),
                                    edge_swap_pct,
                                    source=source_edge,
                                )
                                self._last_alerts[source_edge] = now
                                self._last_alert_at = now

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

        # 主動緊急急救：當主節點告警時，立即清理重量級快取以避開 OOM 崩潰
        if source == "Droplet (主節點)":
            try:
                from services.market_data_service import (
                    clear_history_cache,
                    clear_options_cache,
                )

                clear_history_cache()
                clear_options_cache()
                gc.collect()
                logger.warning(
                    "🧹 [緊急快取救援] 記憶體告警觸發，已主動清空歷史 K 線與期權鏈快取並執行 GC。"
                )
            except Exception as e:
                logger.error(f"Emergency cache eviction failed: {e}")

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
