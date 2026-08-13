"""
cogs/trading/scheduler.py

[Controller] 主排程 Cog：動態市場掃描心跳 (每 30 分鐘)、Reddit 每日更新、
盤中 Scheduled Audit (每 120 分鐘)。
業務邏輯委派給 MarketScanCog、HeartbeatCog helper 及其他子 Cog。
"""

from typing import Any
import asyncio
import logging
from datetime import time
from zoneinfo import ZoneInfo

from discord.ext import tasks, commands

import database
import market_time

ny_tz = ZoneInfo("America/New_York")
logger = logging.getLogger(__name__)

scanner_times = [
    time(hour=h, minute=m, tzinfo=ny_tz) for h in range(24) for m in (0, 30)
]


class SchedulerCog(commands.Cog):
    """
    [Controller] 主排程任務調度器。
    負責「何時執行」，將實際業務邏輯委派給各子 Cog。
    """

    def __init__(self, bot: Any) -> None:
        self.bot = bot

        # Intraday decision scan pipeline (Real-time Phase B SPEAR/Vanna warnings)
        from market_analysis.intraday_pipeline import (
            IntradayScanPipeline,
            NexusGammaSqueezeEngine,
        )

        self.intraday_pipeline = IntradayScanPipeline(
            bot, NexusGammaSqueezeEngine(base_gate_3_threshold=1000000.0)
        )
        self.intraday_pipeline.start()

        self.dynamic_market_scanner.start()
        self.daily_reddit_update.start()

        logger.info("SchedulerCog loaded. Background tasks started.")

    async def cog_unload(self) -> None:
        self.dynamic_market_scanner.cancel()
        self.daily_reddit_update.cancel()
        self.intraday_pipeline.stop()
        logger.info("SchedulerCog unloaded. Background tasks cancelled.")

    # ==========================================
    # 🚀 Reddit 散戶情緒每日非同步更新 (08:30 ET)
    # ==========================================
    @tasks.loop(time=time(hour=8, minute=30, tzinfo=ny_tz))
    async def daily_reddit_update(self) -> None:
        """08:30：每日更新 Reddit 散戶情緒快取 (低頻率任務)"""
        if not getattr(self.bot, "_is_leader_instance", True):
            return
        if not database.any_user_local_tunnel_enabled():
            logger.info(
                "🕸️ [Daily Update] 本地 Tunnel 已關閉（無任何使用者啟用），跳過 Reddit 情緒快取更新。"
            )
            return

        logger.info("🕸️ [Daily Update] 開始非同步抓取 Reddit 情緒快取...")
        all_watchlists = database.get_all_watchlist()
        symbols = sorted(list(set(row[1] for row in all_watchlists)))

        from services.reddit_service import get_reddit_context
        from database.cache import save_kv_cache

        for sym in symbols:
            try:
                sentiment = await get_reddit_context(sym, limit=5)
                await save_kv_cache(f"reddit_sentiment_{sym}", sentiment)
                logger.info(f"✅ [{sym}] Reddit 情緒快取已更新。")
                await asyncio.sleep(2)
            except Exception as e:
                logger.error(f"[{sym}] 每日 Reddit 更新失敗: {e}")

    @daily_reddit_update.before_loop
    async def before_daily_reddit_update(self) -> None:
        await self.bot.wait_until_ready()

    # ==========================================
    # 🕒 盤中動態巡邏 (每 30 分鐘心跳)
    # ==========================================
    @tasks.loop(time=scanner_times)
    async def dynamic_market_scanner(self) -> None:
        """盤中動態巡邏：每 30 分鐘心跳檢查，僅在盤中執行掃描"""
        if not getattr(self.bot, "_is_leader_instance", True):
            return
        if not market_time.is_market_open():
            return

        logger.info("🕒 [盤中掃描] 美股交易時段內，啟動動態雷達並更新大盤總經快取...")

        # 1. 抓取 SPX, VIX, US10Y, WTI 數據並存入 SQLite
        try:
            from market_analysis.dark_pool_engine import fetch_and_cache_darkpool_dix
            from services.market_data_service import get_vix_term_structure, get_quote
            from market_analysis.index_microstructure import fetch_core_macro_metrics

            spx_q, vix_q, tnx_q, wti_q, vts_q, _, _ = await asyncio.gather(
                get_quote("^SPX"),
                get_quote("^VIX"),
                get_quote("^TNX"),
                get_quote("CL=F"),
                get_vix_term_structure(),
                fetch_and_cache_darkpool_dix(),
                fetch_core_macro_metrics(),
                return_exceptions=True,
            )

            spx_val = spx_q.get("c", 0.0) if isinstance(spx_q, dict) else 0.0
            vix_val = vix_q.get("c", 0.0) if isinstance(vix_q, dict) else 0.0
            tnx_val = tnx_q.get("c", 0.0) if isinstance(tnx_q, dict) else 0.0
            wti_val = wti_q.get("c", 0.0) if isinstance(wti_q, dict) else 0.0
            vts_val = vts_q.get("vts_ratio", 0.0) if isinstance(vts_q, dict) else 0.0

            if spx_val > 0.0:
                await database.save_kv_cache("macro_spx", spx_val)
            if vix_val > 0.0:
                await database.save_kv_cache("macro_vix", vix_val)
            if tnx_val > 0.0:
                await database.save_kv_cache("macro_us10y", tnx_val)
            if wti_val > 0.0:
                await database.save_kv_cache("macro_wti", wti_val)
            if vts_val > 0.0:
                await database.save_kv_cache("macro_vts_ratio", vts_val)

            logger.info(
                f"🕒 [盤中總經快取更新完成] SPX: {spx_val}, VIX: {vix_val}, "
                f"US10Y: {tnx_val}, WTI: {wti_val}, VTS: {vts_val}, "
                "DarkPool DIX & Core Metrics updated"
            )

            # 🚨 偵測 VIX 期限結構倒掛與黑天鵝預警
            if vix_val >= 30.0 or vts_val >= 1.0:
                from database import get_all_user_ids, is_notification_enabled
                from cogs.embed_builders.alert_embeds import create_vix_tail_risk_embed

                uids = get_all_user_ids()
                for uid in uids:
                    if is_notification_enabled(uid, "vix_tail_risk_alert"):
                        embed = create_vix_tail_risk_embed(
                            vts_ratio=vts_val, vix=vix_val
                        )
                        await self.bot.queue_dm(uid, embed=embed)

        except Exception as e:
            logger.error(f"🕒 [盤中總經快取更新失敗]: {e}")

        all_watchlists = database.get_all_watchlist()

        # 2. Watchlist 心跳推送
        from cogs.trading.heartbeat import dispatch_watchlist_heartbeat

        await dispatch_watchlist_heartbeat(self.bot, all_watchlists)

        # 3. NRO 掃描邏輯
        scan_cog = self.bot.get_cog("MarketScanCog")
        if scan_cog:
            await scan_cog._run_market_scan_logic(is_auto=True)
        else:
            logger.error("MarketScanCog not found, skipping market scan.")

    @dynamic_market_scanner.before_loop
    async def before_dynamic_market_scanner(self) -> None:
        await self.bot.wait_until_ready()
        logger.info("盤中動態巡邏機已掛載，將每 30 分鐘偵測一次開盤狀態。")


async def setup(bot: Any) -> None:  # type: ignore
    await bot.add_cog(SchedulerCog(bot))
