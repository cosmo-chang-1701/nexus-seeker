"""
cogs/trading/after_market.py

盤後結算報告排程 (16:15 ET) 及共用 pipeline 邏輯。
"""

from typing import Any
import logging
from datetime import datetime, time
from zoneinfo import ZoneInfo

import discord
from discord.ext import tasks, commands

import database
import market_time
from services.trading_service import TradingService
from cogs.embed_builder import create_portfolio_report_embed

ny_tz = ZoneInfo("America/New_York")
logger = logging.getLogger(__name__)


class AfterMarketCog(commands.Cog):
    """盤後結算報告排程與 pipeline。"""

    def __init__(self, bot: Any) -> None:
        self.bot = bot
        self.trading_service = TradingService(bot)
        self.dynamic_after_market_report.start()

    async def cog_unload(self) -> None:
        self.dynamic_after_market_report.cancel()

    @tasks.loop(time=time(hour=16, minute=15, tzinfo=ny_tz))
    async def dynamic_after_market_report(self) -> None:
        """16:15：持倉結算與防禦維護（盤後報告統一由 AnalystAgent.dispatch_post_market_intelligence 發送）"""
        if not getattr(self.bot, "_is_leader_instance", True):
            return
        now_ny = datetime.now(ny_tz)
        today = now_ny.date()

        schedule = market_time.nyse_calendar.schedule(start_date=today, end_date=today)
        if schedule.empty:
            return

        logger.info("Starting dynamic_after_market_report maintenance task.")

        try:
            purged_rows = database.purge_old_cache(days=30)
            logger.info(
                f"🧹 financials_cache 清理完成，刪除 {purged_rows} 筆 30 天前資料"
            )
        except Exception as e:
            logger.warning(f"financials_cache 清理失敗: {e}")

    @dynamic_after_market_report.before_loop
    async def before_dynamic_after_market_report(self) -> None:
        await self.bot.wait_until_ready()

    async def _run_after_market_report_pipeline(
        self, dry_run: bool = False, triggered_by: Any = None
    ) -> Any:
        """共用盤後報告流程：支援排程與手動 dry-run。"""
        if not dry_run:
            analyst_cog = self.bot.get_cog("AnalystAgent")
            if analyst_cog and hasattr(
                analyst_cog, "dispatch_post_market_intelligence"
            ):
                await analyst_cog.dispatch_post_market_intelligence()
                return {
                    "users_total": len(database.get_all_user_ids()),
                    "users_queued": len(database.get_all_user_ids()),
                    "users_skipped": 0,
                    "users_failed": 0,
                    "errors": [],
                }
        mode = "DRY-RUN" if dry_run else "SEND"
        stats: dict[str, Any] = {
            "users_total": 0,
            "users_queued": 0,
            "users_skipped": 0,
            "users_failed": 0,
            "errors": [],
        }

        logger.info(f"[AfterMarketReport] Start pipeline mode={mode}")

        try:
            user_reports = await self.trading_service.get_after_market_report_data()
        except Exception:
            logger.exception("盤後報告資料彙整失敗，本輪略過發送。")
            return stats

        stats["users_total"] = len(user_reports)
        logger.info(
            f"[AfterMarketReport] mode={mode}, users_total={stats['users_total']}"
        )

        for uid, data in user_reports.items():
            report_lines = data.get("report_lines", [])
            hedge_analysis = data.get("hedge_analysis", {})
            survival_runway = data.get("survival_runway")

            try:
                embed = create_portfolio_report_embed(
                    report_lines, hedge_analysis, survival_runway
                )
            except Exception:
                stats["users_failed"] += 1
                err = f"embed_build_failed: uid={uid}"
                stats["errors"].append(err)
                logger.exception(f"建立盤後報告 Embed 失敗，uid={uid}")
                continue

            position_chars = len(embed.fields[0].value) if len(embed.fields) >= 1 else 0
            macro_chars = len(embed.fields[1].value) if len(embed.fields) >= 2 else 0
            hedge_chars = len(embed.fields[2].value) if len(embed.fields) >= 3 else 0
            logger.info(
                f"[AfterMarketReport] uid={uid}, mode={mode}, lines={len(report_lines)}, "
                f"fields={len(embed.fields)}, chars=({position_chars},{macro_chars},{hedge_chars})"
            )

            if dry_run:
                stats["users_skipped"] += 1
                continue

            try:
                user = await self.bot.fetch_user(uid)
            except discord.NotFound:
                stats["users_skipped"] += 1
                logger.warning(f"盤後報告略過：找不到用戶 uid={uid}")
                continue
            except discord.Forbidden:
                stats["users_skipped"] += 1
                logger.warning(f"盤後報告略過：無權限讀取用戶 uid={uid}")
                continue
            except Exception:
                stats["users_failed"] += 1
                err = f"fetch_user_failed: uid={uid}"
                stats["errors"].append(err)
                logger.exception(f"盤後報告 fetch_user 失敗，uid={uid}")
                continue

            if not user:
                stats["users_skipped"] += 1
                logger.warning(f"盤後報告略過：fetch_user 回傳空值，uid={uid}")
                continue

            try:
                if database.is_notification_enabled(uid, "briefing_post_market"):
                    await self.bot.queue_dm(uid, embed=embed)
                    stats["users_queued"] += 1
                    logger.info(f"盤後風險結算報告已排入 DM 佇列，uid={uid}")
                else:
                    logger.info(f"盤後風險結算報告已被使用者關閉，跳過，uid={uid}")
            except discord.Forbidden:
                stats["users_skipped"] += 1
                logger.warning(f"無法發送私訊給用戶 {uid}")
            except Exception as e:
                stats["users_failed"] += 1
                err = f"queue_dm_failed: uid={uid}, err={e}"
                stats["errors"].append(err)
                logger.error(f"發送盤後報告失敗，uid={uid}：{e}")

        if stats["errors"]:
            logger.warning(
                f"[AfterMarketReport] mode={mode}, errors={stats['errors'][:10]}"
            )

        logger.info(
            f"[AfterMarketReport] Finished mode={mode}, "
            f"users_total={stats['users_total']}, users_queued={stats['users_queued']}, "
            f"users_skipped={stats['users_skipped']}, users_failed={stats['users_failed']}"
        )

        if triggered_by and stats["errors"]:
            await triggered_by.send(
                "⚠️ force_after_report 已完成，但有錯誤。\n"
                f"錯誤摘要: `{'; '.join(stats['errors'][:5])}`"
            )
        return stats


async def setup(bot: Any) -> None:
    await bot.add_cog(AfterMarketCog(bot))
