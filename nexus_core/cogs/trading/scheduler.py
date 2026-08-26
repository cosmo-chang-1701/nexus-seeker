"""
cogs/trading/scheduler.py

[Controller] 主排程 Cog：動態市場掃描心跳 (每 15 分鐘)、Reddit 每日更新、
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
    time(hour=h, minute=m, tzinfo=ny_tz) for h in range(24) for m in (0, 15, 30, 45)
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
        self.kv_cache_dedup_purge.start()

        logger.info("SchedulerCog loaded. Background tasks started.")

    async def cog_unload(self) -> None:
        self.dynamic_market_scanner.cancel()
        self.daily_reddit_update.cancel()
        self.kv_cache_dedup_purge.cancel()
        self.intraday_pipeline.stop()
        logger.info("SchedulerCog unloaded. Background tasks cancelled.")

    # ==========================================
    # 🚀 Reddit 散戶情緒每日非同步更新 (08:30 ET)
    # ==========================================
    @tasks.loop(time=time(hour=8, minute=30, tzinfo=ny_tz))
    async def daily_reddit_update(self) -> None:
        if not getattr(self.bot, "_is_leader_instance", True):
            return

        import config

        if not getattr(config, "TUNNEL_URL", ""):
            logger.info(
                "🕸️ [Daily Update] TUNNEL_URL 未配置，跳過 Reddit 情緒快取更新。"
            )
            return

        logger.info("🕸️ [Daily Update] 開始非同步抓取 Reddit 情緒快取...")
        all_watchlists = database.get_all_watchlist()
        symbols = sorted(list(set(row[1] for row in all_watchlists)))

        from services.reddit_service import get_reddit_context_batch
        from database.cache import save_kv_cache

        # 一次抓取版塊最新貼文清單、本地端關鍵字比對，取代逐標的各打一次請求，
        # 大幅降低對 Reddit 的請求數量與 429 風險。
        results = await get_reddit_context_batch(symbols, limit_per_symbol=5)
        for sym, sentiment in results.items():
            try:
                await save_kv_cache(f"reddit_sentiment_{sym}", sentiment)
                logger.info(f"✅ [{sym}] Reddit 情緒快取已更新。")
            except Exception as e:
                logger.error(f"[{sym}] 每日 Reddit 更新失敗: {e}")

    @daily_reddit_update.before_loop
    async def before_daily_reddit_update(self) -> None:
        await self.bot.wait_until_ready()

    # ==========================================
    # 🧹 kv_cache 每日去重旗標清理 (03:00 ET，離峰時段)
    # ==========================================
    @tasks.loop(time=time(hour=3, minute=0, tzinfo=ny_tz))
    async def kv_cache_dedup_purge(self) -> None:
        """清除 kv_cache 中已用完即棄的一次性每日去重旗標（如告警防重複發送
        標記），避免此表隨時間無界成長。僅限白名單前綴，不影響其他任何具持久
        意義的快取（詳見 database/cache.py 的 _KV_CACHE_DEDUP_KEY_PREFIXES）。
        """
        if not getattr(self.bot, "_is_leader_instance", True):
            return

        from database.cache import purge_stale_kv_cache_dedup_keys

        try:
            purged_prefixes = await purge_stale_kv_cache_dedup_keys()
            logger.info(
                f"🧹 [kv_cache 清理] 已清除 {purged_prefixes} 個前綴下的過期去重旗標。"
            )
        except Exception as e:
            logger.error(f"kv_cache 每日去重旗標清理失敗: {e}")

    @kv_cache_dedup_purge.before_loop
    async def before_kv_cache_dedup_purge(self) -> None:
        await self.bot.wait_until_ready()

    # ==========================================
    # 🕒 盤中動態巡邏 (每 15 分鐘心跳)
    # ==========================================
    @tasks.loop(time=scanner_times)
    async def dynamic_market_scanner(self) -> None:
        """盤中動態巡邏：每 15 分鐘心跳檢查，僅在盤中執行掃描"""
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

            # 🛡️ 數據合理性檢驗 (Sanity Check) 與快取回退
            is_vix_valid = 5.0 <= vix_val <= 150.0
            if not is_vix_valid:
                cached_vix = database.get_kv_cache("macro_vix")
                if cached_vix:
                    try:
                        cached_vix_val = float(cached_vix)
                        if 5.0 <= cached_vix_val <= 150.0:
                            vix_val = cached_vix_val
                            is_vix_valid = True
                            logger.info(
                                f"🕒 [VIX 快取回退] 即時報價異常，使用 SQLite 歷史快取 VIX: {vix_val}"
                            )
                    except (ValueError, TypeError):
                        pass

            is_vts_valid = (
                isinstance(vts_q, dict)
                and vts_q.get("is_valid", False)
                and (0.5 <= vts_val <= 3.0)
                and (vts_q.get("vts_state") != "UNKNOWN")
            )

            if spx_val > 0.0:
                await database.save_kv_cache("macro_spx", spx_val)
            if is_vix_valid:
                await database.save_kv_cache("macro_vix", vix_val)
            if tnx_val > 0.0:
                await database.save_kv_cache("macro_us10y", tnx_val)
            if wti_val > 0.0:
                await database.save_kv_cache("macro_wti", wti_val)
            if is_vts_valid:
                await database.save_kv_cache("macro_vts_ratio", vts_val)

            logger.info(
                f"🕒 [盤中總經快取更新完成] SPX: {spx_val}, VIX: {vix_val} (Valid: {is_vix_valid}), "
                f"US10Y: {tnx_val}, WTI: {wti_val}, VTS: {vts_val} (Valid: {is_vts_valid}), "
                "DarkPool DIX & Core Metrics updated"
            )

            # 🚨 偵測 VIX 期限結構倒掛與黑天鵝預警 (嚴格數據把關 + 雙重條件門檻)
            # 條件 A: VIX 實質飆升 >= 30.0 (且 VIX 數據有效)
            # 條件 B: VTS 有效且嚴重倒掛 (VTS >= 1.10) 且短期恐慌已顯著升溫 (VIX >= 20.0)
            is_vix_panic = is_vix_valid and vix_val >= 30.0
            is_vts_inversion_panic = (
                is_vix_valid and is_vts_valid and vts_val >= 1.10 and vix_val >= 20.0
            )

            if is_vix_panic or is_vts_inversion_panic:
                from datetime import datetime, timezone
                from database import get_all_user_ids, is_notification_enabled
                from cogs.embed_builders.alert_embeds import create_vix_tail_risk_embed

                trigger_reason = (
                    f"VIX 飆升至 {vix_val:.1f} (突破 30.0 極端恐慌線)"
                    if is_vix_panic
                    else f"VIX 期限結構嚴重倒掛 (VTS: {vts_val:.2f} >= 1.10) 且 VIX: {vix_val:.1f}"
                )

                today_str = datetime.now(timezone.utc).strftime("%Y%m%d")
                uids = get_all_user_ids()
                for uid in uids:
                    if is_notification_enabled(uid, "defense_macro_tail_risk"):
                        cooldown_key = f"macro_tail_risk_alert_{uid}_{today_str}"
                        if database.get_kv_cache(cooldown_key):
                            continue

                        embed = create_vix_tail_risk_embed(
                            vts_ratio=vts_val if is_vts_valid else 0.0,
                            vix=vix_val,
                            trigger_reason=trigger_reason,
                        )
                        await self.bot.queue_dm(uid, embed=embed)
                        await database.save_kv_cache(cooldown_key, 1)
                        logger.warning(
                            f"🦇 [黑天鵝/尾部風險警報已發送] 使用者: {uid}, 原因: {trigger_reason}"
                        )
            elif not is_vix_valid and (
                not isinstance(vix_q, dict) or vix_q.get("c", 0.0) <= 0.0
            ):
                logger.warning(
                    f"⚠️ [VIX Tail Risk] VIX 即時報價異常或抓取失敗 (VIX: {vix_val})，安全阻斷黑天鵝預警觸發。"
                )

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
        logger.info("盤中動態巡邏機已掛載，將每 15 分鐘偵測一次開盤狀態。")


async def setup(bot: Any) -> None:
    await bot.add_cog(SchedulerCog(bot))
