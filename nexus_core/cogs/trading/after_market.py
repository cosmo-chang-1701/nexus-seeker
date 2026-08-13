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
from config import get_vix_tier
from services.trading_service import TradingService
from services.market_data_service import get_macro_environment, get_quote
from services.llm_service import generate_analyst_report
from cogs.embed_builder import (
    create_portfolio_report_embed,
    create_ai_analysis_embed,
)

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
        """16:15：持倉結算與防禦建議 (依使用者分發私訊)"""
        if not getattr(self.bot, "_is_leader_instance", True):
            return
        now_ny = datetime.now(ny_tz)
        today = now_ny.date()

        schedule = market_time.nyse_calendar.schedule(start_date=today, end_date=today)
        if schedule.empty:
            return

        logger.info("Starting dynamic_after_market_report task.")

        try:
            purged_rows = database.purge_old_cache(days=30)
            logger.info(
                f"🧹 financials_cache 清理完成，刪除 {purged_rows} 筆 30 天前資料"
            )
        except Exception as e:
            logger.warning(f"financials_cache 清理失敗，略過不影響盤後報告: {e}")

        await self._run_after_market_report_pipeline(dry_run=False)

    @dynamic_after_market_report.before_loop
    async def before_dynamic_after_market_report(self) -> None:
        await self.bot.wait_until_ready()

    async def _run_after_market_report_pipeline(
        self, dry_run: bool = False, triggered_by: Any = None
    ) -> Any:
        """共用盤後報告流程：支援排程與手動 dry-run。"""
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
            macro: dict[str, Any] = await get_macro_environment()
            vix = macro.get("vix", 18.0)
            vix_tier = get_vix_tier(vix)
            spy_data = await get_quote("SPY")
            spy_price = spy_data.get("c", 500.0)
            time_str = datetime.now().strftime("%Y-%m-%d")
        except Exception as e:
            logger.warning(f"Macro 數據獲取失敗，部分 AI 功能可能受限: {e}")
            vix = 18.0
            vix_tier = get_vix_tier(vix)
            spy_price = 500.0
            time_str = datetime.now().strftime("%Y-%m-%d")

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

            ai_enabled = False
            ai_report = None
            if not dry_run:
                try:
                    user_ctx = database.get_full_user_context(uid)
                    if user_ctx.enable_analyst_agent:
                        ai_enabled = True
                        raw_data = {
                            "macro_snapshot": {
                                "vix": vix,
                                "vix_tier": vix_tier,
                                "spy_price": spy_price,
                            },
                            "brinson_attribution_proxy": {
                                "total_net_pnl": round(
                                    hedge_analysis.get("net_pnl", 0), 2
                                ),
                                "alpha_selection_pnl": round(
                                    hedge_analysis.get("alpha_contribution", 0), 2
                                ),
                                "market_hedge_pnl": round(
                                    hedge_analysis.get("hedge_contribution", 0), 2
                                ),
                            },
                            "aggregate_risk_metrics": {
                                "total_theta": round(user_ctx.total_theta, 2),
                                "total_beta_delta": round(
                                    user_ctx.total_weighted_delta, 2
                                ),
                                "portfolio_heat_pct": round(
                                    (
                                        abs(user_ctx.total_weighted_delta)
                                        * spy_price
                                        / user_ctx.capital
                                        * 100
                                    )
                                    if user_ctx.capital > 0
                                    else 0,
                                    2,
                                ),
                                "avg_financial_runway_days": round(
                                    survival_runway
                                    if survival_runway is not None
                                    else 0,
                                    1,
                                ),
                            },
                            "sector_correlation": "Stable",
                        }
                        report_type = f"{time_str} 盤後交易與每日總結"
                        logger.info(f"正在為用戶 {uid} 生成 AI 盤後分析報告...")
                        ai_report = await generate_analyst_report(report_type, raw_data)
                except Exception as ai_e:
                    logger.error(f"AI 報告生成失敗 (uid={uid})，改用預設標題: {ai_e}")

            try:
                embed_runway = None if ai_enabled else survival_runway
                embed = create_portfolio_report_embed(
                    report_lines, hedge_analysis, embed_runway
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
                if ai_enabled and ai_report:
                    if database.is_notification_enabled(uid, "briefing_post_market"):
                        ai_embed = create_ai_analysis_embed(ai_report)
                        await self.bot.queue_dm(uid, embed=ai_embed)
                        logger.info(f"盤後 AI 深度分析 Embed 已排入 DM 佇列，uid={uid}")

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


async def setup(bot: Any) -> None:  # type: ignore
    await bot.add_cog(AfterMarketCog(bot))
