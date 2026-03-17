import discord
from discord.ext import tasks, commands
from discord import app_commands
import asyncio
import time as _time
from datetime import datetime, time
from zoneinfo import ZoneInfo
from typing import Dict
import logging

import database
import market_time
from config import DISCORD_ADMIN_USER_ID
from services.trading_service import TradingService
from services.alert_filter import should_send_priority_alert, is_whipsaw_noise
from cogs.embed_builder import create_scan_embed, build_vtr_stats_embed, create_portfolio_report_embed, create_rehedge_embed

ny_tz = ZoneInfo("America/New_York")
logger = logging.getLogger(__name__)

class SchedulerCog(commands.Cog):
    """
    [Controller] 背景排程任務與私訊分發引擎。
    僅負責「何時執行」與「如何展現結果」，核心業務邏輯委派給 TradingService。
    """

    def __init__(self, bot):
        self.bot = bot
        self.trading_service = TradingService(bot)
        
        # 啟動背景任務
        self.pre_market_risk_monitor.start()
        self.dynamic_market_scanner.start()
        self.dynamic_after_market_report.start()
        self.monitor_vtr_task.start()
        self.weekly_vtr_report_task.start()

        # 狀態與設定 (由 Cog 維護，與 Discord 狀態相關)
        self.signal_cooldowns = {}
        self.COOLDOWN_HOURS = 4
        self.EARNINGS_WARNING_DAYS = 14
        self.last_notified_target = None

        # 防重複旗標：記錄每個動態排程任務最後一次成功執行的日期，
        # 避免 change_interval 與 tasks.loop 內部計時器競爭導致同一天觸發兩次。
        self._pre_market_last_run_date = None
        self._after_market_last_run_date = None

        # 🚀 宏觀環境快照：用於 AlertFilter 比對 VIX 變動幅度
        self.prev_macro_state: Dict[str, float] = {}
        
        logger.info("SchedulerCog loaded. Background tasks started.")

    def cog_unload(self):
        """卸載 Cog 時取消所有背景任務。"""
        self.pre_market_risk_monitor.cancel()
        self.dynamic_market_scanner.cancel()
        self.dynamic_after_market_report.cancel()
        self.monitor_vtr_task.cancel()
        self.weekly_vtr_report_task.cancel()
        logger.info("SchedulerCog unloaded. Background tasks cancelled.")

    # ==========================================
    # 🚀 每週 VTR 績效週報 (美東週五 17:05)
    # ==========================================
    @tasks.loop(time=time(hour=17, minute=5, tzinfo=ny_tz))
    async def weekly_vtr_report_task(self):
        """每週五收盤後：自動推送 VTR 績效週報"""
        now = datetime.now(ny_tz)
        if now.weekday() != 4: # 4 代表 Friday
            return

        logger.info("📅 [Weekly Report] 偵測到週五收盤，開始產生績效週報...")
        
        all_watchlists = database.get_all_watchlist()
        unique_users = set(row[0] for row in all_watchlists)

        for uid in unique_users:
            try:
                from market_analysis.ghost_trader import GhostTrader
                stats = GhostTrader.get_vtr_performance_stats(uid)
                if stats['total_trades'] > 0:
                    user = await self.bot.fetch_user(uid)
                    embed = build_vtr_stats_embed(user.display_name, stats)
                    await self.bot.queue_dm(uid, message="📊 **本週虛擬交易室 (VTR) 績效週報已送達！**", embed=embed)
                    logger.info(f"✅ 週報已發送給用戶 {uid}")
            except Exception as e:
                logger.error(f"發送週報給 {uid} 失敗: {e}")

    # ==========================================
    # 動態排程任務 (私訊分發引擎)
    # ==========================================
    @tasks.loop(hours=24)
    async def pre_market_risk_monitor(self):
        """09:00：盤前財報警報 (依使用者分發私訊)"""
        logger.info("Starting pre_market_risk_monitor task.")
        try:
            now_ny = datetime.now(market_time.ny_tz)
            today = now_ny.date()

            # 防重複：同一日期只執行一次，避免 change_interval 競爭導致重複觸發
            if self._pre_market_last_run_date == today:
                logger.info("pre_market_risk_monitor already ran today, skipping.")
                return

            target_time = market_time.get_next_market_target_time(reference="open", offset_minutes=-30)

            # 非交易日會拿到下一個交易日的 target，當天直接略過。
            if not target_time or target_time.date() != today:
                return

            self._pre_market_last_run_date = today
            
            results = await self.trading_service.get_pre_market_alerts_data(self.EARNINGS_WARNING_DAYS)
            
            for uid, data in results.items():
                alerts = []
                for item in data['alerts']:
                    status = "⚠️ **持倉高風險**" if item['is_portfolio'] else "👀 觀察清單"
                    alerts.append(f"**{item['symbol']}** ({status})\n└ 📅 財報日: `{item['earnings_date']}` (倒數 **{item['days_left']}** 天)")

                user = await self.bot.fetch_user(uid)
                if user:
                    if alerts:
                        embed = discord.Embed(title="🚨 【盤前財報季雷達預警】", description="\n\n".join(alerts), color=discord.Color.red())
                    else:
                        scanned_list = "、".join([f"`{s}`" for s in data['scanned_symbols']])
                        embed = discord.Embed(title="✅ 【盤前財報季雷達掃描完畢】", description=f"已掃描：{scanned_list}\n\n近 {self.EARNINGS_WARNING_DAYS} 日內無財報風險，安全過關！", color=discord.Color.green())
                    
                    try:
                        await self.bot.queue_dm(uid, embed=embed)
                    except discord.Forbidden:
                        pass
        finally:
            # 任務結束後，進行「帶有通知」的重排程
            try:
                await self._reschedule_pre_market_risk(send_notify=True)
            except Exception:
                logger.exception("盤前警報重排程失敗。")

    async def _reschedule_pre_market_risk(self, send_notify: bool = True):
        """計算下一個盤前目標時間並動態調整排程間隔。"""
        target_time = market_time.get_next_market_target_time(reference="open", offset_minutes=-30)
        if not target_time:
            logger.warning("找不到下一個盤前目標時間，1 小時後重試排程。")
            self.pre_market_risk_monitor.change_interval(hours=1)
            return None

        # 只有在非啟動初始化時才發送通知
        if send_notify:
            await self._notify_next_schedule("盤前財報警報", target_time)

        sleep_seconds = market_time.get_sleep_seconds(target_time)
        self.pre_market_risk_monitor.change_interval(seconds=max(30.0, sleep_seconds))
        return target_time

    @pre_market_risk_monitor.before_loop
    async def before_pre_market_risk_monitor(self):
        await self.bot.wait_until_ready()
        await self._reschedule_pre_market_risk(send_notify=False)

    @tasks.loop(minutes=30)
    async def dynamic_market_scanner(self):
        """盤中動態巡邏：每 30 分鐘心跳檢查，僅在盤中 (09:45後) 執行掃描"""
        target_time = market_time.get_next_market_target_time(reference="open", offset_minutes=15)
        
        if target_time and target_time != self.last_notified_target:
            await self._notify_next_schedule("盤中動態掃描", target_time)
            self.last_notified_target = target_time

        if not market_time.is_market_open():
            return
                
        now_ny = datetime.now(market_time.ny_tz)
        if now_ny.hour == 9: # 09:30 - 09:59 避開
            return

        logger.info("🕒 [盤中掃描] 美股交易時段內，啟動動態雷達...")
        await self._run_market_scan_logic(is_auto=True)

    @dynamic_market_scanner.before_loop
    async def before_dynamic_market_scanner(self):
        await self.bot.wait_until_ready()
        logger.info("盤中動態巡邏機已掛載，將每 30 分鐘偵測一次開盤狀態。")

    @app_commands.command(name="force_scan", description="[Admin] 立即手動執行全站掃描 (不論開盤時間)")
    async def force_scan(self, interaction: discord.Interaction):
        if interaction.user.id != DISCORD_ADMIN_USER_ID:
            await interaction.response.send_message("⛔ 權限不足：此指令僅限管理員使用。", ephemeral=True)
            logger.warning(f"Unauthorized force_scan attempt by {interaction.user.name} ({interaction.user.id})")
            return

        logger.info(f"Admin {interaction.user.name} ({interaction.user.id}) triggered force_scan")
        await interaction.response.send_message("🚀 強制啟動全站掃描中...", ephemeral=True)
        asyncio.create_task(self._run_market_scan_logic(is_auto=False, triggered_by=interaction.user))

    @app_commands.command(name="force_after_report", description="[Admin] 立即手動執行盤後結算報告 (可選 dry-run)")
    @app_commands.describe(dry_run="true=只做計算與建構，不發送 DM")
    async def force_after_report(self, interaction: discord.Interaction, dry_run: bool = True):
        if interaction.user.id != DISCORD_ADMIN_USER_ID:
            await interaction.response.send_message("⛔ 權限不足：此指令僅限管理員使用。", ephemeral=True)
            logger.warning(f"Unauthorized force_after_report attempt by {interaction.user.name} ({interaction.user.id})")
            return

        mode = "DRY-RUN" if dry_run else "SEND"
        logger.info(f"Admin {interaction.user.name} ({interaction.user.id}) triggered force_after_report mode={mode}")
        await interaction.response.send_message(
            f"🧪 盤後結算報告手動執行中 (`{mode}`)...",
            ephemeral=True,
        )

        stats = await self._run_after_market_report_pipeline(dry_run=dry_run, triggered_by=interaction.user)
        await interaction.followup.send(
            (
                "✅ 盤後結算流程完成\n"
                f"mode: `{mode}`\n"
                f"users_total: `{stats['users_total']}`\n"
                f"users_queued: `{stats['users_queued']}`\n"
                f"users_skipped: `{stats['users_skipped']}`\n"
                f"users_failed: `{stats['users_failed']}`"
            ),
            ephemeral=True,
        )

    async def _run_after_market_report_pipeline(self, dry_run: bool = False, triggered_by=None):
        """共用盤後報告流程：支援排程與手動 dry-run。"""
        mode = "DRY-RUN" if dry_run else "SEND"
        stats = {
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
        logger.info(f"[AfterMarketReport] mode={mode}, users_total={stats['users_total']}")

        for uid, data in user_reports.items():
            report_lines = data.get("report_lines", [])
            hedge_analysis = data.get("hedge_analysis")

            try:
                embed = create_portfolio_report_embed(report_lines, hedge_analysis)
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
                await self.bot.queue_dm(uid, message="📊 **【Nexus Seeker 盤後結算系統】**", embed=embed)
                stats["users_queued"] += 1
                logger.info(f"盤後報告已排入 DM 佇列，uid={uid}，fields={len(embed.fields)}")
            except discord.Forbidden:
                stats["users_skipped"] += 1
                logger.warning(f"無法發送私訊給用戶 {uid}")
            except Exception:
                stats["users_failed"] += 1
                err = f"queue_dm_failed: uid={uid}"
                stats["errors"].append(err)
                logger.exception(f"盤後報告排入 DM 佇列失敗，uid={uid}")

        if stats["errors"]:
            logger.warning(f"[AfterMarketReport] mode={mode}, errors={stats['errors'][:10]}")

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

    async def _run_market_scan_logic(self, is_auto=True, triggered_by=None):
        """共用的掃描核心邏輯，協調 Service 計算與 Discord 訊息發送。"""
        try:
            if not is_auto and triggered_by:
                await triggered_by.send("🔍 **開始掃描標的...**")

            # 呼叫 Service 執行核心計算
            user_results = await self.trading_service.run_market_scan(
                is_auto=is_auto, 
                triggered_by_id=triggered_by.id if triggered_by else None
            )

            if not user_results:
                if not is_auto and triggered_by:
                    await triggered_by.send("📭 **本次掃描未發現符合策略的交易機會或觀察清單為空。**")
                return

            now = datetime.now(ny_tz)
            for uid, alerts_data in user_results.items():
                user_cooldowns = self.signal_cooldowns.setdefault(uid, {})
                valid_alerts = []

                for data in alerts_data:
                    sym = data['symbol']
                    ai_decision = data.get('ai_decision', 'APPROVE')

                    # 攔截邏輯：VETO 絕對不建倉
                    if ai_decision == "VETO":
                        continue 
                    
                    # 冷卻檢查 (僅在自動模式下)
                    if is_auto:
                        last_sent_time = user_cooldowns.get(sym)
                        if last_sent_time:
                            time_diff = (now - last_sent_time).total_seconds()
                            if time_diff < (self.COOLDOWN_HOURS * 3600):
                                continue 

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
                    for sig in data.get('ema_signals', []):
                        if sig.get('type') == 'CROSSOVER':
                            database.update_watchlist_alert_state(
                                uid, sym,
                                direction=sig['direction'],
                                price=data.get('price', 0.0),
                                timestamp=int(_time.time()),
                            )
                            break  # 每次掃描只記錄第一個通過的 CROSSOVER

                    # 將推播理由注入 data，供 Embed 顯示
                    if reason:
                        data['alert_reason'] = reason
                    
                    valid_alerts.append(data)
                    if is_auto:
                        user_cooldowns[sym] = now
                        # 執行 VTR 自動建倉
                        await self.trading_service.execute_vtr_auto_entry(data)

                if valid_alerts:
                    title = "📡 **【盤中動態掃描】NRO 風控已介入判定：**" if is_auto else "⚡ **【管理員強制掃描】風險模擬結果：**"
                    await self.bot.queue_dm(uid, message=title)
                    user_capital = database.get_full_user_context(uid).capital
                    for data in valid_alerts:
                        await self.bot.queue_dm(uid, embed=create_scan_embed(data, user_capital))
                        
                        # 🛡️ 檢查是否有自動回補避險建議
                        rehedge_info = data.get('rehedge_info')
                        if rehedge_info:
                            await self.bot.queue_dm(uid, embed=create_rehedge_embed(rehedge_info))

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
                vix = data.get('macro_vix')
                if vix is not None:
                    self.prev_macro_state['vix'] = vix
                    logger.debug(f"[MacroState] 快照已更新: VIX={vix:.2f}")
                    return  # VIX 是全域值，取到一筆即可

    @tasks.loop(hours=24)
    async def dynamic_after_market_report(self):
        """16:15：持倉結算與防禦建議 (依使用者分發私訊)"""
        logger.info("Starting dynamic_after_market_report task.")
        try:
            now_ny = datetime.now(market_time.ny_tz)
            today = now_ny.date()

            # 防重複：同一日期只執行一次，避免 change_interval 競爭導致重複觸發
            if self._after_market_last_run_date == today:
                logger.info("dynamic_after_market_report already ran today, skipping.")
                return

            self._after_market_last_run_date = today

            # 盤後順帶清理過舊財務快取，維持資料庫體積與查詢效率
            try:
                purged_rows = database.purge_old_cache(days=30)
                logger.info(f"🧹 financials_cache 清理完成，刪除 {purged_rows} 筆 30 天前資料")
            except Exception as e:
                logger.warning(f"financials_cache 清理失敗，略過不影響盤後報告: {e}")

            await self._run_after_market_report_pipeline(dry_run=False)
        finally:
            # 每輪結束後依交易日行事曆重新對齊下一次執行時間。
            try:
                await self._reschedule_after_market_report(send_notify=True)
            except Exception:
                logger.exception("盤後報告重排程失敗。")

    async def _reschedule_after_market_report(self, send_notify: bool = True):
        target_time = market_time.get_next_market_target_time(reference="close", offset_minutes=15)
        if not target_time:
            logger.warning("找不到下一個盤後目標時間，1 小時後重試排程。")
            self.dynamic_after_market_report.change_interval(hours=1)
            return None

        # 僅在非初始化狀態下發送通知
        if send_notify:
            await self._notify_next_schedule("盤後結算報告", target_time)

        sleep_seconds = market_time.get_sleep_seconds(target_time)
        self.dynamic_after_market_report.change_interval(seconds=max(30.0, sleep_seconds))
        return target_time

    @dynamic_after_market_report.before_loop
    async def before_dynamic_after_market_report(self):
        await self.bot.wait_until_ready()
        await self._reschedule_after_market_report(send_notify=False)

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
                trade_info = res['trade_info']
                hedge = res['hedge']
                uid = res['uid']
                
                status_icon = "🔄 [轉倉完成]" if trade_info['status'] == 'ROLLED' else "🔴 [自動平倉]"
                exposure_pct = (res['current_total_delta'] * res['spy_price'] / res['user_capital']) * 100
                
                msg = (
                    f"{status_icon} **{trade_info['symbol']}** 結算通知\n"
                    f"└ 損益: `${trade_info['pnl']}` | 目前總曝險: `{exposure_pct:.2f}%` \n"
                )

                if hedge:
                    msg += (
                        f"\n🧠 **系統自主位階判定：** `{res['regime']}`\n"
                        f"└ 理想總曝險目標：`{res['target_delta']:.1f} Delta`\n"
                        f"🛡️ **自動對沖決策：** {hedge['action']} (缺口: `{hedge['gap']}`)"
                    )
                
                await self.bot.queue_dm(uid, message=msg)

        except Exception as e:
            logger.error(f"VTR 對沖連動任務錯誤: {e}")
            
    @monitor_vtr_task.before_loop
    async def before_monitor_vtr_task(self):
        await self.bot.wait_until_ready()

    async def _notify_next_schedule(self, task_name, target_time):
        """通知所有使用者下一次任務執行時間"""
        if not target_time:
            return
        unix_ts = int(target_time.timestamp())
        msg = f"📅 **{task_name}** 下次執行時間: <t:{unix_ts}:F> (<t:{unix_ts}:R>)"
        try:
            await self.bot.notify_all_users(msg)
        except Exception as e:
            logger.warning(f"Failed to send schedule notification: {e}")

async def setup(bot):
    await bot.add_cog(SchedulerCog(bot))