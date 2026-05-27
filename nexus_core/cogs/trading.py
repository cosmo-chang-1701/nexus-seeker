import discord
from discord.ext import tasks, commands
from discord import app_commands
import asyncio
import time as _time
from datetime import datetime, time
from zoneinfo import ZoneInfo
from typing import Dict, Any
import logging

import database
import market_time
from config import DISCORD_ADMIN_USER_ID, get_vix_tier
from services.trading_service import TradingService
from services.alert_filter import should_send_priority_alert
from services.market_data_service import get_macro_environment, get_quote
from services.llm_service import generate_analyst_report
from cogs.embed_builder import (
    create_scan_embed,
    build_vtr_stats_embed,
    create_portfolio_report_embed,
    create_rehedge_embed,
    create_ai_analysis_embed,
    create_info_embed,
    create_error_embed,
    create_profit_lock_alert_embed,
    create_gamma_fragility_embed,
    create_pre_market_earnings_embed,
    create_ditm_transition_alert_embed,
    create_vtr_settlement_notice_embed,
    create_watchlist_overview_embed,
    create_watchlist_signal_embed,
)
from market_analysis.ghost_trader import GhostTrader

ny_tz = ZoneInfo("America/New_York")
logger = logging.getLogger(__name__)


class SchedulerCog(commands.Cog):
    """
    [Controller] 背景排程任務與私訊分發引擎。
    僅負責「何時執行」與「如何展現結果」，核心業務邏輯委派給 TradingService。
    """

    def __init__(self, bot: Any) -> None:
        self.bot = bot
        self.trading_service = TradingService(bot)

        # 啟動背景任務
        self.pre_market_risk_monitor.start()
        self.dynamic_market_scanner.start()
        self.dynamic_after_market_report.start()
        self.monitor_vtr_task.start()
        self.monitor_real_portfolio_task.start()
        self.daily_reddit_update.start()
        self.weekly_vtr_report_task.start()

        # 狀態與設定 (由 Cog 維護，與 Discord 狀態相關)
        self.signal_cooldowns: Dict[str, Any] = {}
        self.COOLDOWN_HOURS = 4
        self.EARNINGS_WARNING_DAYS = 14

        # 🚀 宏觀環境快照：用於 AlertFilter 比對 VIX 變動幅度
        self.prev_macro_state: Dict[str, float] = {}

        logger.info("SchedulerCog loaded. Background tasks started.")

    async def cog_unload(self) -> None:
        """卸載 Cog 時取消所有背景任務。"""
        self.pre_market_risk_monitor.cancel()
        self.dynamic_market_scanner.cancel()
        self.dynamic_after_market_report.cancel()
        self.monitor_vtr_task.cancel()
        self.monitor_real_portfolio_task.cancel()
        self.daily_reddit_update.cancel()
        self.weekly_vtr_report_task.cancel()
        logger.info("SchedulerCog unloaded. Background tasks cancelled.")

    # ==========================================
    # 🚀 Reddit 散戶情緒每日非同步更新 (08:30 ET)
    # ==========================================
    @tasks.loop(time=time(hour=8, minute=30, tzinfo=ny_tz))
    async def daily_reddit_update(self):
        """08:30：每日更新 Reddit 散戶情緒快取 (低頻率任務)"""
        logger.info("🕸️ [Daily Update] 開始非同步抓取 Reddit 情緒快取...")
        all_watchlists = database.get_all_watchlist()
        symbols = sorted(list(set(row[1] for row in all_watchlists)))

        from services.reddit_service import get_reddit_context
        from database.cache import save_kv_cache

        for sym in symbols:
            try:
                # 抓取情緒並存入 KV 快取 (key: reddit_sentiment_{symbol})
                sentiment = await get_reddit_context(sym, limit=5)
                save_kv_cache(f"reddit_sentiment_{sym}", sentiment)
                logger.info(f"✅ [{sym}] Reddit 情緒快取已更新。")
                await asyncio.sleep(2)  # 減少 Tunnel 壓力
            except Exception as e:
                logger.error(f"[{sym}] 每日 Reddit 更新失敗: {e}")

    @daily_reddit_update.before_loop
    async def before_daily_reddit_update(self):
        await self.bot.wait_until_ready()

    # ==========================================
    # 🚀 真實持倉風險動態審計
    # ==========================================
    @tasks.loop(minutes=30)
    async def monitor_real_portfolio_task(self):
        """每 30 分鐘審計真實持倉風險 (DITM & Gamma Fragility)"""
        if not market_time.is_market_open():
            return

        logger.info("🛡️ [NRO] 開始執行真實持倉風險審計...")
        try:
            risk_events = await self.trading_service.audit_real_portfolio_risk()

            for event in risk_events:
                uid = event["uid"]
                if event["type"] == "PROFIT_LOCK":
                    embed = create_profit_lock_alert_embed(event)
                    await self.bot.queue_dm(uid, embed=embed)

                elif event["type"] == "GAMMA_FRAGILITY":
                    embed = create_gamma_fragility_embed(event)
                    await self.bot.queue_dm(uid, embed=embed)

        except Exception as e:
            logger.error(f"真實持倉風險審計錯誤: {e}")

    @monitor_real_portfolio_task.before_loop
    async def before_monitor_real_portfolio_task(self):
        await self.bot.wait_until_ready()

    # ==========================================
    # 🚀 每週 VTR 績效週報 (美東週五 17:05)
    # ==========================================
    @tasks.loop(time=time(hour=17, minute=5, tzinfo=ny_tz))
    async def weekly_vtr_report_task(self):
        """每週五收盤後：自動推送 VTR 績效週報"""
        now = datetime.now(ny_tz)
        if now.weekday() != 4:  # 4 代表 Friday
            return

        logger.info("📅 [Weekly Report] 偵測到週五收盤，開始產生績效週報...")

        all_watchlists = database.get_all_watchlist()
        unique_users = set(row[0] for row in all_watchlists)

        for uid in unique_users:
            try:
                stats = await GhostTrader.get_vtr_performance_stats(uid)
                if stats["total_trades"] > 0:
                    user = await self.bot.fetch_user(uid)
                    embed = build_vtr_stats_embed(user.display_name, stats)
                    await self.bot.queue_dm(uid, embed=embed)
                    logger.info(f"✅ 週報已發送給用戶 {uid}")
            except Exception as e:
                logger.error(f"發送週報給 {uid} 失敗: {e}")

    # ==========================================
    # 動態排程任務 (私訊分發引擎)
    # ==========================================
    @tasks.loop(time=time(hour=9, minute=0, tzinfo=ny_tz))
    async def pre_market_risk_monitor(self):
        """09:00：盤前財報警報 (依使用者分發私訊)"""
        now_ny = datetime.now(ny_tz)
        today = now_ny.date()

        # 檢查今天是否為交易日
        schedule = market_time.nyse_calendar.schedule(start_date=today, end_date=today)
        if schedule.empty:
            return

        logger.info("Starting pre_market_risk_monitor task.")
        try:
            results = await self.trading_service.get_pre_market_alerts_data(
                self.EARNINGS_WARNING_DAYS
            )

            for uid, data in results.items():
                user = await self.bot.fetch_user(uid)
                if user:
                    embed = create_pre_market_earnings_embed(
                        data["alerts"],
                        data["scanned_symbols"],
                        self.EARNINGS_WARNING_DAYS,
                    )

                    try:
                        await self.bot.queue_dm(uid, embed=embed)
                    except discord.Forbidden:
                        pass
        except Exception as e:
            logger.error(f"盤前掃描執行錯誤: {e}")

    @pre_market_risk_monitor.before_loop
    async def before_pre_market_risk_monitor(self):
        await self.bot.wait_until_ready()

    @tasks.loop(minutes=30)
    async def dynamic_market_scanner(self):
        """盤中動態巡邏：每 30 分鐘心跳檢查，僅在盤中執行掃描"""
        if not market_time.is_market_open():
            return

        logger.info("🕒 [盤中掃描] 美股交易時段內，啟動動態雷達...")
        all_watchlists = database.get_all_watchlist()
        await self._dispatch_watchlist_heartbeat(all_watchlists)
        await self._run_market_scan_logic(is_auto=True)

    @dynamic_market_scanner.before_loop
    async def before_dynamic_market_scanner(self):
        await self.bot.wait_until_ready()
        logger.info("盤中動態巡邏機已掛載，將每 30 分鐘偵測一次開盤狀態。")

    @app_commands.command(
        name="force_scan", description="[Admin] 立即手動執行全站掃描 (不論開盤時間)"
    )
    async def force_scan(self, interaction: discord.Interaction):
        if interaction.user.id != DISCORD_ADMIN_USER_ID:
            await interaction.response.send_message(
                embed=create_error_embed(
                    "權限不足：此指令僅限管理員使用。", title="權限錯誤"
                ),
                ephemeral=True,
            )
            logger.warning(
                f"Unauthorized force_scan attempt by {interaction.user.name} ({interaction.user.id})"
            )
            return

        logger.info(
            f"Admin {interaction.user.name} ({interaction.user.id}) triggered force_scan"
        )
        await interaction.response.send_message(
            embed=create_info_embed("系統控制", "🚀 強制啟動全站掃描中..."),
            ephemeral=True,
        )
        asyncio.create_task(
            self._run_market_scan_logic(is_auto=False, triggered_by=interaction.user)
        )

    @app_commands.command(
        name="force_after_report",
        description="[Admin] 立即手動執行盤後結算報告 (可選 dry-run)",
    )
    @app_commands.describe(dry_run="true=只做計算與建構，不發送 DM")
    async def force_after_report(
        self, interaction: discord.Interaction, dry_run: bool = True
    ):
        if interaction.user.id != DISCORD_ADMIN_USER_ID:
            await interaction.response.send_message(
                embed=create_error_embed(
                    "權限不足：此指令僅限管理員使用。", title="權限錯誤"
                ),
                ephemeral=True,
            )
            logger.warning(
                f"Unauthorized force_after_report attempt by {interaction.user.name} ({interaction.user.id})"
            )
            return

        mode = "DRY-RUN" if dry_run else "SEND"
        logger.info(
            f"Admin {interaction.user.name} ({interaction.user.id}) triggered force_after_report mode={mode}"
        )
        await interaction.response.send_message(
            embed=create_info_embed(
                "系統控制", f"🧪 盤後結算報告手動執行中 (`{mode}`)..."
            ),
            ephemeral=True,
        )

        stats = await self._run_after_market_report_pipeline(
            dry_run=dry_run, triggered_by=interaction.user
        )
        await interaction.followup.send(
            embed=create_info_embed(
                "盤後結算報告完成",
                (
                    f"mode: `{mode}`\n"
                    f"users_total: `{stats['users_total']}`\n"
                    f"users_queued: `{stats['users_queued']}`\n"
                    f"users_skipped: `{stats['users_skipped']}`\n"
                    f"users_failed: `{stats['users_failed']}`"
                ),
            ),
            ephemeral=True,
        )

    async def _run_after_market_report_pipeline(
        self, dry_run: bool = False, triggered_by=None
    ):
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

        # 🚀 準備宏觀數據與標記時間 (用於 AI 報告)
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

            # 🚀 [Integrated Analyst Agent] 整合 AI 專業點評
            ai_enabled = False
            if not dry_run:
                try:
                    user_ctx = database.get_full_user_context(uid)
                    if user_ctx.enable_analyst_agent:
                        ai_enabled = True
                        # 構建個人化量化數據集
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
                # 如果啟用了 AI 點評，則在 Embed 中隱藏重複的生存跑道資訊
                # AI 報告已包含跑道、歸因與風控評估
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
                # 如果有 AI 報告，先發送 AI 報告 Embed
                if ai_enabled and "ai_report" in locals() and ai_report:
                    ai_embed = create_ai_analysis_embed(ai_report)
                    await self.bot.queue_dm(uid, embed=ai_embed)
                    logger.info(f"盤後 AI 深度分析 Embed 已排入 DM 佇列，uid={uid}")

                # 發送標準風險結算報告 Embed
                await self.bot.queue_dm(uid, embed=embed)
                stats["users_queued"] += 1
                logger.info(f"盤後風險結算報告已排入 DM 佇列，uid={uid}")
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

    @app_commands.command(
        name="ddp_scan", description="立即對觀察清單執行 Davis Double Play (DDP) 掃描"
    )
    async def ddp_scan(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=False)
        all_watchlists = database.get_all_watchlist()
        symbols = sorted(list(set(row[1] for row in all_watchlists)))

        if not symbols:
            await interaction.followup.send(
                embed=create_info_embed(
                    "觀察清單", "📭 觀察清單為空，無法執行 DDP 掃描。"
                )
            )
            return

        results = await self.trading_service.run_ddp_scan(symbols)
        if not results:
            await interaction.followup.send(
                embed=create_info_embed(
                    "DDP 掃描結果",
                    "🔎 掃描完成，目前沒有符合 Davis Double Play (DDP) 條件的標的。",
                )
            )
            return

        for report in results:
            from cogs.embed_builder import create_ddp_embed

            embed = create_ddp_embed(report)
            await interaction.followup.send(embed=embed)
            # 同時存入資料庫
            self.trading_service.ddp_inspector.record_signal(report)

    @app_commands.command(
        name="iv_scan", description="立即對觀察清單執行波動率優勢 (Cheap IV) 偵測"
    )
    async def iv_scan(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=False)
        all_watchlists = database.get_all_watchlist()
        # 為每個用戶獨立掃描 (因為 Runway Impact 不同)
        uids = sorted(list(set(row[0] for row in all_watchlists)))

        if not uids:
            await interaction.followup.send(
                embed=create_info_embed(
                    "觀察清單", "📭 觀察清單為空，無法執行 IV 掃描。"
                )
            )
            return

        found_any = False
        for uid in uids:
            user_watch = [row[1] for row in all_watchlists if row[0] == uid]
            results = await self.trading_service.run_iv_opportunity_scan(
                user_watch, uid
            )

            for report in results:
                from cogs.embed_builder import create_volatility_embed

                embed = create_volatility_embed(report)
                if interaction.user.id == uid:
                    await interaction.followup.send(embed=embed)
                else:
                    await self.bot.queue_dm(uid, embed=embed)
                found_any = True

        if not found_any:
            await interaction.followup.send(
                embed=create_info_embed(
                    "IV 掃描結果",
                    "🔎 掃描完成，目前沒有符合波動率優勢 (Cheap IV) 條件的標的。",
                )
            )

    async def _should_send_alert(self, uid: int, symbol: str, alert_mode: int) -> bool:
        """
        根據使用者的警報模式與標的是否在持倉中，決定是否發送通知。
        0=OFF, 1=ALL, 2=PORTFOLIO_ONLY
        """
        if alert_mode == 0:
            return False
        if alert_mode == 1:
            return True
        if alert_mode == 2:
            return database.is_symbol_in_portfolio(uid, symbol)
        return True

    async def _dispatch_watchlist_heartbeat(
        self, all_watchlists: list[tuple[int, str, int]] | None = None
    ) -> None:
        """每個 30 分鐘節點推送 watchlist 全標的技術 / 期權 / skew 戰報。"""
        from market_analysis.intraday_pipeline import (
            build_watchlist_skew_commentary_payload,
            build_watchlist_option_plan,
            derive_watchlist_option_guidance,
            evaluate_watchlist_symbol,
        )
        from services.calendar_service import calendar_service
        from services.llm_service import (
            generate_watchlist_roundup_commentary,
            generate_watchlist_skew_commentary,
        )
        from ui.formatter import generate_ansi_watchlist_report
        from services import market_data_service

        if all_watchlists is None:
            all_watchlists = database.get_all_watchlist()
        if not all_watchlists:
            return

        unique_symbols = sorted({sym for _, sym, _ in all_watchlists})
        macro_event, earnings_map, df_spy = await asyncio.gather(
            calendar_service.get_next_high_impact_event(days=7),
            calendar_service.get_symbol_earnings_batch(unique_symbols),
            market_data_service.get_spy_history_df(period="1y"),
        )
        evaluations = await asyncio.gather(
            *(
                evaluate_watchlist_symbol(
                    symbol,
                    earnings_event=earnings_map.get(symbol),
                    macro_event=macro_event,
                    df_spy=df_spy,
                )
                for symbol in unique_symbols
            )
        )
        evaluation_map = {
            evaluation.metrics.symbol: evaluation
            for evaluation in evaluations
            if evaluation is not None
        }
        skew_commentary_map: Dict[str, str] = {}
        if evaluation_map:
            skew_commentaries = await asyncio.gather(
                *(
                    generate_watchlist_skew_commentary(
                        symbol,
                        build_watchlist_skew_commentary_payload(evaluation),
                    )
                    for symbol, evaluation in evaluation_map.items()
                )
            )
            skew_commentary_map = dict(
                zip(evaluation_map.keys(), skew_commentaries, strict=False)
            )

        user_symbols: Dict[int, list[str]] = {}
        for uid, sym, _ in all_watchlists:
            user_symbols.setdefault(uid, [])
            if sym not in user_symbols[uid]:
                user_symbols[uid].append(sym)

        for uid, symbols in user_symbols.items():
            user_context = database.get_full_user_context(uid)
            option_alert_mode = int(getattr(user_context, "option_alert_mode", 1))
            user_holdings = {
                str(row.get("symbol", "")).upper(): row
                for row in database.get_user_holdings(uid)
            }
            summary_items: list[dict[str, str]] = []
            deliverable_symbols: list[tuple[str, bool]] = []
            for sym in symbols:
                evaluation = evaluation_map.get(sym)
                if evaluation is None:
                    continue
                has_position = database.is_symbol_in_portfolio(uid, sym)
                if option_alert_mode == 2 and not has_position:
                    continue
                deliverable_symbols.append((sym, has_position))

                sum_holding_row = user_holdings.get(sym.upper())
                sum_holding_pnl_pct = None
                if (
                    sum_holding_row is not None
                    and float(sum_holding_row.get("quantity", 0.0)) > 0.0
                ):
                    sum_holding_avg_cost = float(sum_holding_row.get("avg_cost", 0.0))
                    if sum_holding_avg_cost > 0.0:
                        sum_holding_pnl_pct = (
                            evaluation.metrics.current_price - sum_holding_avg_cost
                        ) / sum_holding_avg_cost

                item_data = {
                    "symbol": sym,
                    "alert_level": str(evaluation.tactical.alert_level),
                    "skew_state": (
                        f"{evaluation.metrics.option_skew:+.2f}% ｜ "
                        f"{evaluation.metrics.option_skew_state}"
                    ),
                    "scenario": str(evaluation.tactical.scenario),
                    "event_risk_summary": str(evaluation.event_context.summary),
                }
                if sum_holding_pnl_pct is not None:
                    item_data["holding_pnl_pct"] = sum_holding_pnl_pct

                summary_items.append(item_data)

            if summary_items:
                roundup_commentary = await generate_watchlist_roundup_commentary(
                    {
                        "symbols_total": len(summary_items),
                        "symbols": [
                            {
                                **item,
                                "skew_commentary": skew_commentary_map.get(
                                    item["symbol"],
                                    "",
                                ),
                            }
                            for item in summary_items
                        ],
                    }
                )
                overview_embed = create_watchlist_overview_embed(
                    summary_items,
                    llm_overview=roundup_commentary,
                )
                await self.bot.queue_dm(uid, embed=overview_embed)

            for sym, has_position in deliverable_symbols:
                evaluation = evaluation_map.get(sym)
                if evaluation is None:
                    continue
                holding_row = user_holdings.get(sym.upper())
                holding_quantity: float | None = None
                holding_avg_cost: float | None = None
                holding_pnl_pct: float | None = None
                if (
                    holding_row is not None
                    and float(holding_row.get("quantity", 0.0)) > 0.0
                ):
                    holding_quantity = float(holding_row["quantity"])
                    holding_avg_cost = float(holding_row.get("avg_cost", 0.0))
                    if holding_avg_cost > 0.0:
                        holding_pnl_pct = (
                            evaluation.metrics.current_price - holding_avg_cost
                        ) / holding_avg_cost
                report_body = generate_ansi_watchlist_report(
                    evaluation.metrics, evaluation.tactical
                )
                option_guidance = derive_watchlist_option_guidance(
                    evaluation.metrics,
                    evaluation.tactical,
                    event_context=evaluation.event_context,
                    has_position=has_position,
                )
                option_plan = await build_watchlist_option_plan(
                    evaluation.metrics,
                    evaluation.tactical,
                    capital=user_context.capital,
                    risk_limit=user_context.risk_limit,
                    event_context=evaluation.event_context,
                )
                embed = create_watchlist_signal_embed(
                    symbol=sym,
                    report_body=report_body,
                    option_guidance=option_guidance,
                    event_risk_summary=evaluation.event_context.summary,
                    skew_state=(
                        f"{evaluation.metrics.option_skew:+.2f}% ｜ "
                        f"{evaluation.metrics.option_skew_state}"
                    ),
                    alert_level=evaluation.tactical.alert_level,
                    option_plan=option_plan,
                    skew_commentary=skew_commentary_map.get(sym),
                    has_position=has_position,
                    holding_quantity=holding_quantity,
                    holding_avg_cost=holding_avg_cost,
                    holding_pnl_pct=holding_pnl_pct,
                )
                await self.bot.queue_dm(uid, embed=embed)

    async def _run_market_scan_logic(self, is_auto=True, triggered_by=None):
        """共用的掃描核心邏輯，協調 Service 計算與 Discord 訊息發送。"""
        try:
            if not is_auto and triggered_by:
                await triggered_by.send("🔍 **開始掃描標的...**")

            # 🚀 1. 執行 DDP 掃描 (Davis Double Play)
            all_watchlists = database.get_all_watchlist()
            symbols_all = sorted(list(set(row[1] for row in all_watchlists)))
            if symbols_all:
                ddp_results = await self.trading_service.run_ddp_scan(symbols_all)
                for report in ddp_results:
                    from cogs.embed_builder import create_ddp_embed

                    embed = create_ddp_embed(report)
                    sym = report["symbol"]

                    # ⚠️ 這裡原本會發給全站用戶，現在修正為僅發給 Watchlist 中有該標的且符合模式的用戶
                    for uid, watch_sym, _ in all_watchlists:
                        if watch_sym == sym:
                            ctx = database.get_full_user_context(uid)
                            if await self._should_send_alert(
                                uid, sym, ctx.option_alert_mode
                            ):
                                await self.bot.queue_dm(uid, embed=embed)

                    self.trading_service.ddp_inspector.record_signal(report)

            # 🚀 2. 執行 IV 優勢掃描 (Volatility Strategist)
            uids = sorted(list(set(row[0] for row in all_watchlists)))
            for uid in uids:
                user_context = database.get_full_user_context(uid)
                user_watch = [row[1] for row in all_watchlists if row[0] == uid]
                vol_results = await self.trading_service.run_iv_opportunity_scan(
                    user_watch, uid
                )
                for report in vol_results:
                    if await self._should_send_alert(
                        uid, report["symbol"], user_context.option_alert_mode
                    ):
                        from cogs.embed_builder import create_volatility_embed

                        embed = create_volatility_embed(report)
                        await self.bot.queue_dm(uid, embed=embed)

            # 🚀 3. 執行標準 NRO 掃描
            user_results = await self.trading_service.run_market_scan(
                is_auto=is_auto,
                triggered_by_id=triggered_by.id if triggered_by else None,
            )

            if not user_results:
                if not is_auto and triggered_by:
                    await triggered_by.send(
                        "📭 **本次掃描未發現符合策略的交易機會或觀察清單為空。**"
                    )
                return

            now = datetime.now(ny_tz)
            for uid, alerts_data in user_results.items():
                user_cooldowns = self.signal_cooldowns.setdefault(uid, {})
                valid_alerts = []

                user_context = database.get_full_user_context(uid)
                for data in alerts_data:
                    sym = data["symbol"]
                    ai_decision = data.get("ai_decision", "APPROVE")
                    alert_type = data.get("alert_type", "OPTION")
                    cooldown_key = f"{sym}_{alert_type}"

                    # 攔截邏輯：VETO 絕對不建倉
                    if ai_decision == "VETO":
                        continue

                    # 冷卻檢查 (僅在自動模式下)
                    if is_auto:
                        last_sent_time = user_cooldowns.get(cooldown_key)
                        if last_sent_time:
                            time_diff = (now - last_sent_time).total_seconds()
                            if time_diff < (self.COOLDOWN_HOURS * 3600):
                                continue

                    if alert_type == "OPTION":
                        # 🚀 條件式過濾 (AlertFilter 訊號降噪 + 防騙線)
                        # 從資料庫取得上次 CROSSOVER 觸發狀態，傳入 AlertFilter
                        last_alert_state = database.get_watchlist_alert_state(uid, sym)
                        is_priority, reason = await should_send_priority_alert(
                            data, self.prev_macro_state, last_alert_state
                        )

                        if is_auto and not is_priority:
                            logger.info(f"⏭️ 標的 {sym} 未達優先通知門檻，已過濾。")
                            continue

                        # 🛡️ 若通過過濾且包含 CROSSOVER 訊號，更新資料庫狀態
                        for sig in data.get("ema_signals", []):
                            if sig.get("type") == "CROSSOVER":
                                database.update_watchlist_alert_state(
                                    uid,
                                    sym,
                                    direction=sig["direction"],
                                    price=data.get("price", 0.0),
                                    timestamp=int(_time.time()),
                                )
                                break  # 每次掃描只記錄第一個通過的 CROSSOVER

                        # 將推播理由注入 data，供 Embed 顯示
                        if reason:
                            data["alert_reason"] = reason

                        if await self._should_send_alert(
                            uid, sym, user_context.option_alert_mode
                        ):
                            valid_alerts.append(data)

                            # 🚀 立即發送執行決策 (SDDM)
                            exec_decision = data.get("execution_decision")
                            if exec_decision:
                                from formatters.execution_embeds import (
                                    build_execution_embed,
                                )

                                await self.bot.queue_dm(
                                    uid, embed=build_execution_embed(exec_decision)
                                )

                        if is_auto:
                            user_cooldowns[cooldown_key] = now
                            # 執行 VTR 自動建倉
                            if user_context.enable_vtr:
                                await self.trading_service.execute_vtr_auto_entry(data)

                    elif alert_type == "PSQ":
                        if await self._should_send_alert(
                            uid, sym, user_context.option_alert_mode
                        ):
                            valid_alerts.append(data)
                        if is_auto:
                            user_cooldowns[cooldown_key] = now

                if valid_alerts:
                    title = (
                        "📡 **【盤中動態掃描】NRO 風控已介入判定：**"
                        if is_auto
                        else "⚡ **【管理員強制掃描】風險模擬結果：**"
                    )
                    await self.bot.queue_dm(
                        uid,
                        embed=create_info_embed(title="掃描通知", message=title),
                    )
                    user_capital = user_context.capital
                    for data in valid_alerts:
                        if data.get("alert_type") == "PSQ":
                            from cogs.embed_builder import create_psq_embed

                            await self.bot.queue_dm(uid, embed=create_psq_embed(data))
                        else:
                            await self.bot.queue_dm(
                                uid, embed=create_scan_embed(data, user_capital)
                            )

                            # 🛡️ 檢查是否有自動回補避險建議
                            rehedge_info = data.get("rehedge_info")
                            if rehedge_info:
                                await self.bot.queue_dm(
                                    uid, embed=create_rehedge_embed(rehedge_info)
                                )

            # 🚀 掃描結束後更新宏觀環境快照，供下一輪 AlertFilter 比對
            self._update_macro_state(user_results)

        except Exception as e:
            logger.error(f"掃描邏輯執行錯誤: {e}")

    def _update_macro_state(self, user_results: Dict[int, list]):
        """
        從本輪掃描結果中提取宏觀環境快照 (VIX)，
        存入 prev_macro_state 供下一輪 AlertFilter 比對變動幅度。
        """
        for alerts_data in user_results.values():
            for data in alerts_data:
                vix = data.get("macro_vix")
                if vix is not None:
                    self.prev_macro_state["vix"] = vix
                    logger.debug(f"[MacroState] 快照已更新: VIX={vix:.2f}")
                    return  # VIX 是全域值，取到一筆即可

    @tasks.loop(time=time(hour=16, minute=15, tzinfo=ny_tz))
    async def dynamic_after_market_report(self):
        """16:15：持倉結算與防禦建議 (依使用者分發私訊)"""
        now_ny = datetime.now(ny_tz)
        today = now_ny.date()

        # 檢查今天是否為交易日
        schedule = market_time.nyse_calendar.schedule(start_date=today, end_date=today)
        if schedule.empty:
            return

        logger.info("Starting dynamic_after_market_report task.")

        # 盤後順帶清理過舊財務快取，維持資料庫體積與查詢效率
        try:
            purged_rows = database.purge_old_cache(days=30)
            logger.info(
                f"🧹 financials_cache 清理完成，刪除 {purged_rows} 筆 30 天前資料"
            )
        except Exception as e:
            logger.warning(f"financials_cache 清理失敗，略過不影響盤後報告: {e}")

        await self._run_after_market_report_pipeline(dry_run=False)

    @dynamic_after_market_report.before_loop
    async def before_dynamic_after_market_report(self):
        await self.bot.wait_until_ready()

    # ==========================================
    # 🚀 VTR 監控與風險即時預警
    # ==========================================
    @tasks.loop(minutes=30)
    async def monitor_vtr_task(self):
        """每 30 分鐘檢查 VTR，並在轉倉/平倉時即時通知"""
        if not market_time.is_market_open():
            return

        logger.info("👻 [GhostTrader] 開始掃描 VTR 持倉與風險檢查...")
        try:
            results = await self.trading_service.monitor_vtr_and_calculate_hedging()

            for res in results:
                trade_info = res["trade_info"]
                hedge = res["hedge"]
                uid = res["uid"]
                tags = trade_info.get("tags", [])

                # 偵測是否為 DITM 防禦事件
                is_ditm = any("DITM" in str(tag) for tag in tags)

                if is_ditm:
                    exit_reason = next(
                        (
                            tag.split(":", 1)[1]
                            for tag in tags
                            if tag.startswith("exit_reason:")
                        ),
                        "N/A",
                    )
                    action_taken = (
                        "已平倉 (Closed)"
                        if trade_info["status"] == "CLOSED"
                        else "已自動轉倉 (向上/向後轉倉)"
                    )

                    exposure_pct = (
                        res["current_total_delta"]
                        * res["spy_price"]
                        / res["user_capital"]
                    ) * 100
                    embed = create_ditm_transition_alert_embed(
                        symbol=trade_info["symbol"],
                        exit_reason=exit_reason,
                        action_taken=action_taken,
                        pnl=float(trade_info["pnl"]),
                        exposure_pct=exposure_pct,
                        hedge=hedge,
                    )
                    await self.bot.queue_dm(uid, embed=embed)
                else:
                    status_icon = (
                        "🔄 [轉倉完成]"
                        if trade_info["status"] == "ROLLED"
                        else "🔴 [自動平倉]"
                    )
                    exposure_pct = (
                        res["current_total_delta"]
                        * res["spy_price"]
                        / res["user_capital"]
                    ) * 100

                    await self.bot.queue_dm(
                        uid,
                        embed=create_vtr_settlement_notice_embed(
                            status_icon=status_icon,
                            symbol=trade_info["symbol"],
                            pnl=float(trade_info["pnl"]),
                            exposure_pct=exposure_pct,
                            regime=res.get("regime"),
                            target_delta=res.get("target_delta"),
                            hedge=hedge,
                        ),
                    )

        except Exception as e:
            logger.error(f"VTR 對沖連動任務錯誤: {e}")

    @monitor_vtr_task.before_loop
    async def before_monitor_vtr_task(self):
        await self.bot.wait_until_ready()


async def setup(bot):
    await bot.add_cog(SchedulerCog(bot))
