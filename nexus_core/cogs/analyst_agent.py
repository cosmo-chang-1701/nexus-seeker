"""Analyst Agent Cog — scheduling orchestration and DM dispatch.

Business logic for each report domain lives in the runner sub-modules under
``market_analysis/analyst_runners/``.  This cog owns:
  - Task loop scheduling (pre-market, intraday, post-market)
  - dispatch_report  : generic multi-user embed broadcast
  - dispatch_intraday_guide : delegates to intraday_runner
  - dispatch_pre_market_briefing : orchestrates macro + earnings embed delivery
  - dispatch_post_market_intelligence : orchestrates sector + LLM post-market delivery
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone, timedelta
from typing import Optional

import discord
from discord.ext import commands, tasks

import database
from market_time import (
    get_next_market_target_time,
    get_sleep_seconds,
    is_market_open,
)
from services.llm_service import generate_analyst_report
from cogs.embed_builder import split_embed_by_fields

# ── Runner sub-modules ────────────────────────────────────────────────────────
from market_analysis.analyst_runners import macro_runner
from market_analysis.analyst_runners import earnings_runner
from market_analysis.analyst_runners import sector_runner
from market_analysis.analyst_runners import portfolio_runner
from market_analysis.analyst_runners import strategy_runner
from market_analysis.analyst_runners import intraday_runner

# Re-export SECTORS so existing tests and callers can still do
# ``from cogs.analyst_agent import SECTORS``
from market_analysis.analyst_runners.sector_runner import SECTORS  # noqa: F401

logger = logging.getLogger(__name__)


class AnalystAgent(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.pre_market_loop.start()
        # intra_day_loop is integrated into trading.py's intraday_decision_scan
        # self.intra_day_loop.start()
        self.post_market_loop.start()

    def cog_unload(self):
        self.pre_market_loop.cancel()
        # self.intra_day_loop.cancel()
        self.post_market_loop.cancel()

    # ──────────────────────────────────────────────────────────────────────────
    # Utility helpers
    # ──────────────────────────────────────────────────────────────────────────

    def _get_tw_time_str(self) -> str:
        """Return current Taiwan time (UTC+8) as a formatted tag."""
        now_tw = datetime.now(timezone.utc).astimezone(timezone(timedelta(hours=8)))
        return now_tw.strftime("[%H:%M UTC+8]")

    async def _fetch_macro_data(self) -> dict:
        """Thin wrapper around macro_runner.fetch_macro_data for test-mock compatibility."""
        return await macro_runner.fetch_macro_data()

    # ──────────────────────────────────────────────────────────────────────────
    # Scheduler loops
    # ──────────────────────────────────────────────────────────────────────────

    # ==========================================
    # 🚀 1. 盤前總覽：開盤前 30 分鐘啟動
    # ==========================================
    @tasks.loop(count=1)
    async def pre_market_loop(self):
        await self.bot.wait_until_ready()
        while True:
            target = get_next_market_target_time("open", offset_minutes=-30)
            sleep_secs = get_sleep_seconds(target)
            logger.info(
                f"🤖 [Analyst Pre-Market] 下次執行時間: {target} (倒數 {sleep_secs/3600:.2f} 小時)"
            )
            await asyncio.sleep(sleep_secs)

            try:
                if not getattr(self.bot, "_is_leader_instance", True):
                    await asyncio.sleep(30)
                    continue
                logger.info("🤖 [Analyst Pre-Market] 啟動盤前綜合宏觀與自選股報告...")
                await self.dispatch_pre_market_briefing()
            except Exception as e:
                logger.error(f"Analyst Pre-Market loop error: {e}")

            await asyncio.sleep(60)

    # ==========================================
    # 🚀 2. 盤中監測：每 120 分鐘心跳掃描 (僅開盤時)
    # ==========================================
    @tasks.loop(count=1)
    async def intra_day_loop(self):
        await self.bot.wait_until_ready()
        while True:
            if is_market_open():
                if not getattr(self.bot, "_is_leader_instance", True):
                    await asyncio.sleep(30)
                    continue
                logger.info(
                    "🤖 [Analyst Intra-Day] 偵測到開盤，執行 120 分鐘心跳掃描..."
                )
                try:
                    await self.dispatch_intraday_guide()
                except Exception as e:
                    logger.error(f"Analyst Intra-Day loop error: {e}")
                await asyncio.sleep(120 * 60)
            else:
                target = get_next_market_target_time("open", offset_minutes=0)
                sleep_secs = get_sleep_seconds(target)
                logger.info(
                    f"🤖 [Analyst Intra-Day] 市場休市。下次開盤心跳: {target} (倒數 {sleep_secs/3600:.2f} 小時)"
                )
                await asyncio.sleep(min(sleep_secs, 3600))

    # ==========================================
    # 🚀 3. 盤後策略：收盤後 15 分鐘啟動
    # ==========================================
    @tasks.loop(count=1)
    async def post_market_loop(self):
        await self.bot.wait_until_ready()
        while True:
            target = get_next_market_target_time("close", offset_minutes=15)
            sleep_secs = get_sleep_seconds(target)
            logger.info(
                f"🤖 [Analyst Post-Market] 下次執行時間: {target} (倒數 {sleep_secs/3600:.2f} 小時)"
            )
            await asyncio.sleep(sleep_secs)

            try:
                if not getattr(self.bot, "_is_leader_instance", True):
                    await asyncio.sleep(30)
                    continue
                logger.info(
                    "🤖 [Analyst Post-Market] 啟動盤後綜合風險與 AI 策略報告流程..."
                )
                await self.dispatch_post_market_intelligence()
            except Exception as e:
                logger.error(f"Analyst Post-Market loop error: {e}")

            await asyncio.sleep(60)

    # ──────────────────────────────────────────────────────────────────────────
    # Generic broadcast
    # ──────────────────────────────────────────────────────────────────────────

    async def dispatch_report(
        self, report_content: discord.Embed, notification_key: str = None
    ):
        """Broadcast an embed to all users who have the given notification key enabled."""
        report_embeds = split_embed_by_fields(report_content)
        user_ids = database.get_all_user_ids()
        dispatched_count = 0
        for uid in user_ids:
            if notification_key and not database.is_notification_enabled(
                uid, notification_key
            ):
                logger.info(
                    f"使用者 {uid} 已關閉 {notification_key} 訂閱，略過本次推送。"
                )
                continue
            try:
                for embed in report_embeds:
                    await self.bot.queue_dm(uid, embed=embed)
                    dispatched_count += 1
            except Exception as e:
                logger.error(f"Failed to dispatch report to {uid}: {e}")
        logger.info(f"Dispatched report to {dispatched_count} users.")

    # ──────────────────────────────────────────────────────────────────────────
    # Intraday execution guide
    # ──────────────────────────────────────────────────────────────────────────

    async def dispatch_intraday_guide(self):
        """Delegate per-user intraday execution guide to intraday_runner."""
        await intraday_runner.run_intraday_guide(
            self.bot, fetch_macro_fn=self._fetch_macro_data
        )

    # ──────────────────────────────────────────────────────────────────────────
    # Pre-market briefing
    # ──────────────────────────────────────────────────────────────────────────

    async def dispatch_pre_market_briefing(self):
        # 0. 盤前更新 FedWatch 數據
        try:
            from services.calendar_service import calendar_service

            await calendar_service.update_fedwatch_probability()
        except Exception as e:
            logger.warning(f"更新 FedWatch 概率失敗: {e}")

        # 1. 宏觀資料
        macro_data = await self._fetch_macro_data()
        if isinstance(macro_data, tuple):
            vix, dxy, tnx = macro_data
            macro_data = {
                "vix": vix,
                "vix_change": 0.0,
                "dxy": dxy,
                "tnx": tnx,
                "tnx_change_bps": 0.0,
                "us2y": tnx - 0.2,
            }

        macro_alerts = macro_runner.build_macro_alerts(macro_data)

        # 2. 財報預警資料
        warning_days = 2
        from services.trading_service import TradingService

        ts = TradingService(self.bot)
        user_earnings_data = await ts.get_pre_market_alerts_data(
            warning_days=warning_days
        )

        user_ids = database.get_all_user_ids()
        from cogs.embed_builder import build_pre_market_briefing_embed

        for uid in user_ids:
            if not database.is_notification_enabled(uid, "pre_market_briefing"):
                continue
            u_data = user_earnings_data.get(uid, {"alerts": [], "scanned_symbols": []})

            formatted_alerts = []
            for alert in u_data["alerts"]:
                e_date_str = (
                    alert["earnings_date"].strftime("%Y-%m-%d")
                    if isinstance(alert["earnings_date"], datetime)
                    or hasattr(alert["earnings_date"], "strftime")
                    else str(alert["earnings_date"])
                )
                formatted_alerts.append(
                    {
                        "symbol": alert["symbol"],
                        "is_portfolio": alert["is_portfolio"],
                        "earnings_date": e_date_str,
                        "days_left": alert["days_left"],
                    }
                )

            embed = build_pre_market_briefing_embed(
                macro_data=macro_data,
                alerts=macro_alerts,
                earnings_alerts=formatted_alerts,
                scanned_symbols=u_data["scanned_symbols"],
                warning_days=warning_days,
            )
            await self.bot.queue_dm(uid, embed=embed)

            # FOMC 逃頂窗口
            try:
                fomc_embed = await self.run_fomc_escape_window_analysis(uid)
                if fomc_embed:
                    await self.bot.queue_dm(uid, embed=fomc_embed)
            except Exception as e:
                logger.error(f"推送方案 C 逃頂窗口 Embed 失敗: {e}")

    # ──────────────────────────────────────────────────────────────────────────
    # Post-market intelligence
    # ──────────────────────────────────────────────────────────────────────────

    async def dispatch_post_market_intelligence(self):
        # 1. 清除舊快取
        try:
            purged_rows = database.purge_old_cache(days=30)
            logger.info(
                f"🧹 financials_cache 清理完成，刪除 {purged_rows} 筆 30 天前資料"
            )
        except Exception as e:
            logger.warning(f"financials_cache 清理失敗: {e}")

        # 2. 板塊輪動資料 (run once for all users)
        sector_rotation_data = await self.gather_sector_rotation_data()

        # 3. 用戶投資組合風險指標
        from services.trading_service import TradingService

        ts = TradingService(self.bot)
        try:
            user_reports = await ts.get_after_market_report_data()
        except Exception as e:
            logger.error(f"Error gathering after market report data: {e}")
            user_reports = {}

        user_ids = database.get_all_user_ids()
        import psutil
        from cogs.embed_builder import build_post_market_intelligence_embed

        for uid in user_ids:
            if not database.is_notification_enabled(uid, "post_market_intelligence"):
                continue

            user_ctx = database.get_full_user_context(uid)
            if not user_ctx.enable_analyst_agent:
                continue

            u_report = user_reports.get(uid, {})
            report_lines = u_report.get("report_lines", [])
            hedge_analysis = u_report.get("hedge_analysis", {})
            survival_runway = u_report.get("survival_runway")
            if survival_runway is None:
                from market_analysis.pro_management import calculate_survival_runway

                survival_runway = calculate_survival_runway(
                    cash_reserve=user_ctx.cash_reserve,
                    monthly_expense=user_ctx.monthly_expense,
                    daily_theta=user_ctx.total_theta,
                )

            # 4. 記憶體安全閘
            mem = psutil.virtual_memory()
            if mem.percent > 85.0:
                logger.warning(
                    f"🚨 [Memory Gate] RAM usage ({mem.percent}%) > 85%, "
                    f"AI Commentary suspended for user {uid}"
                )
                ai_commentary = (
                    "⚠️ [Memory Gate] 系統記憶體使用率高於 85%，"
                    "為確保系統穩定，盤後 AI 深度分析與歸因點評已暫停。"
                )
            else:
                raw_data = {
                    "macro_snapshot": {
                        "vix": sector_rotation_data["vix"],
                        "vix_tier": sector_rotation_data["vix_tier_name"],
                        "spy_price": sector_rotation_data["spy_price"],
                    },
                    "brinson_attribution_proxy": {
                        "total_net_pnl": round(hedge_analysis.get("net_pnl", 0), 2),
                        "alpha_selection_pnl": round(
                            hedge_analysis.get("alpha_contribution", 0), 2
                        ),
                        "market_hedge_pnl": round(
                            hedge_analysis.get("hedge_contribution", 0), 2
                        ),
                    },
                    "aggregate_risk_metrics": {
                        "total_theta": round(user_ctx.total_theta, 2),
                        "total_beta_delta": round(user_ctx.total_weighted_delta, 2),
                        "portfolio_heat_pct": round(
                            (
                                abs(user_ctx.total_weighted_delta)
                                * sector_rotation_data["spy_price"]
                                / user_ctx.capital
                                * 100
                            )
                            if user_ctx.capital > 0
                            else 0,
                            2,
                        ),
                        "avg_financial_runway_days": round(
                            survival_runway if survival_runway is not None else 0, 1
                        ),
                    },
                    "sectors": sector_rotation_data["sectors"],
                    "poly_events": sector_rotation_data["poly_events"],
                    "spy_max_pain": sector_rotation_data["spy_max_pain"],
                }
                time_str = datetime.now().strftime("%Y-%m-%d")
                report_type = f"{time_str} 盤後綜合風險與 AI 策略報告"
                try:
                    ai_commentary = await generate_analyst_report(report_type, raw_data)
                except Exception as e:
                    logger.error(f"Error generating analyst report for user {uid}: {e}")
                    ai_commentary = "⚠️ 無法生成 AI 報告分析。"

            embeds = build_post_market_intelligence_embed(
                report_lines=report_lines,
                hedge_analysis=hedge_analysis,
                survival_runway=survival_runway,
                sectors_data=sector_rotation_data["sectors"],
                ai_commentary=ai_commentary,
            )
            for emb in embeds:
                await self.bot.queue_dm(uid, embed=emb)

    # ──────────────────────────────────────────────────────────────────────────
    # Runner wrappers — thin delegates to runner sub-modules
    # ──────────────────────────────────────────────────────────────────────────

    async def run_macro_scan(self):
        return await macro_runner.run_macro_scan()

    async def run_premarket_earnings(self):
        return await earnings_runner.run_premarket_earnings()

    async def run_market_open_liquidity(self):
        return await sector_runner.run_market_open_liquidity()

    async def run_deep_research(self):
        return await sector_runner.run_deep_research()

    async def run_portfolio_hedging(self):
        return await portfolio_runner.run_portfolio_hedging()

    async def run_postmarket_summary(self):
        return await portfolio_runner.run_postmarket_summary()

    async def run_sector_flow_report(self):
        return await sector_runner.run_sector_flow_report(self.bot)

    async def run_next_day_strategy(self):
        return await strategy_runner.run_next_day_strategy(self._fetch_macro_data)

    async def run_fomc_escape_window_analysis(
        self, user_id: int
    ) -> Optional[discord.Embed]:
        return await strategy_runner.run_fomc_escape_window_analysis(user_id)

    async def gather_sector_rotation_data(self) -> dict:
        return await sector_runner.gather_sector_rotation_data(self.bot)


async def setup(bot):
    await bot.add_cog(AnalystAgent(bot))
