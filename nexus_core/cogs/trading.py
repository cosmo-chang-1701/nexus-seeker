import discord
from discord.ext import tasks, commands
from discord import app_commands
import asyncio
from datetime import datetime, time
from zoneinfo import ZoneInfo
import logging

import database
import market_time
from services.trading_service import TradingService
from cogs.embed_builder import create_scan_embed, build_vtr_stats_embed, create_portfolio_report_embed

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
    @tasks.loop(count=1)
    async def pre_market_risk_monitor(self):
        """09:00：盤前財報警報 (依使用者分發私訊)"""
        logger.info("Starting pre_market_risk_monitor task.")
        target_time = market_time.get_next_market_target_time(reference="open", offset_minutes=-30)
        await self._notify_next_schedule("盤前財報警報", target_time)
        await asyncio.sleep(market_time.get_sleep_seconds(target_time))
        
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

    @pre_market_risk_monitor.before_loop
    async def before_pre_market_risk_monitor(self):
        await self.bot.wait_until_ready()

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
        logger.info(f"Admin {interaction.user.name} ({interaction.user.id}) triggered force_scan")
        await interaction.response.send_message("🚀 強制啟動全站掃描中...", ephemeral=True)
        asyncio.create_task(self._run_market_scan_logic(is_auto=False, triggered_by=interaction.user))

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
                    
                    valid_alerts.append(data)
                    if is_auto:
                        user_cooldowns[sym] = now
                        # 執行 VTR 自動建倉
                        await self.trading_service.execute_vtr_auto_entry(data)

                if valid_alerts:
                    title = "📡 **【盤中動態掃描】NRO 風控已介入判定：**" if is_auto else "⚡ **【管理員強制掃描】風險模擬結果：**"
                    await self.bot.queue_dm(uid, message=title)
                    user_capital = database.get_user_capital(uid) or 50000.0
                    for data in valid_alerts:
                        await self.bot.queue_dm(uid, embed=create_scan_embed(data, user_capital))

        except Exception as e:
            logger.error(f"掃描邏輯執行錯誤: {e}")

    @tasks.loop(count=1)
    async def dynamic_after_market_report(self):
        """16:15：持倉結算與防禦建議 (依使用者分發私訊)"""
        logger.info("Starting dynamic_after_market_report task.")
        target_time = market_time.get_next_market_target_time(reference="close", offset_minutes=15)
        await self._notify_next_schedule("盤後結算報告", target_time)
        await asyncio.sleep(market_time.get_sleep_seconds(target_time))

        user_reports = await self.trading_service.get_after_market_report_data()

        for uid, report_lines in user_reports.items():
            user = await self.bot.fetch_user(uid)
            if user:
                embed = create_portfolio_report_embed(report_lines)
                try:
                    await self.bot.queue_dm(uid, message="📊 **【Nexus Seeker 盤後結算系統】**", embed=embed)
                except discord.Forbidden:
                    logger.warning(f"無法發送私訊給用戶 {uid}")

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