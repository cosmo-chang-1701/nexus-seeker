import discord
from discord.ext import tasks, commands
from discord import app_commands
import asyncio
from datetime import datetime
from zoneinfo import ZoneInfo
import logging

import database
import market_math
import market_time
import market_analysis.portfolio
from cogs.embed_builder import create_scan_embed
import yfinance as yf

ny_tz = ZoneInfo("America/New_York")
logger = logging.getLogger(__name__)

class SchedulerCog(commands.Cog):
    """背景排程任務與私訊分發引擎"""

    def __init__(self, bot):
        self.bot = bot
        self.pre_market_risk_monitor.start()
        self.dynamic_market_scanner.start()
        self.dynamic_after_market_report.start()

        # 4小時冷卻機制
        self.signal_cooldowns = {}
        self.COOLDOWN_HOURS = 4

        self.last_notified_target = None
        logger.info("SchedulerCog loaded. Background tasks started.")

    def cog_unload(self):
        self.pre_market_risk_monitor.cancel()
        self.dynamic_market_scanner.cancel()
        self.dynamic_after_market_report.cancel()
        logger.info("SchedulerCog unloaded. Background tasks cancelled.")

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
        
        today = datetime.now(ny_tz).date()
        
        # 1. 取得全站資料並群組化
        all_portfolios = database.get_all_portfolio()
        all_watchlists = database.get_all_watchlist()
        
        user_symbols = {} # { user_id: { 'port': set(), 'watch': set() } }
        unique_symbols = set()
        
        for row in all_portfolios:
            uid, sym = row[0], row[2]
            user_symbols.setdefault(uid, {'port': set(), 'watch': set()})['port'].add(sym)
            unique_symbols.add(sym)
            
        for row in all_watchlists:
            uid, sym = row[0], row[1]
            user_symbols.setdefault(uid, {'port': set(), 'watch': set()})['watch'].add(sym)
            unique_symbols.add(sym)

        # 2. 批次快取財報日期 (減少重複 API 請求)
        earnings_cache = {}
        for sym in unique_symbols:
            ticker = yf.Ticker(sym)
            e_date = await asyncio.to_thread(market_math.get_next_earnings_date, ticker)
            if e_date:
                if isinstance(e_date, datetime): e_date = e_date.date()
                earnings_cache[sym] = e_date

        # 3. 組合並發送私訊給每位使用者
        for uid, symbols_data in user_symbols.items():
            alerts = []
            combined_symbols = symbols_data['port'].union(symbols_data['watch'])
            
            for sym in combined_symbols:
                e_date = earnings_cache.get(sym)
                if e_date:
                    days_left = (e_date - today).days
                    if 0 <= days_left <= 3:
                        status = "⚠️ **持倉高風險**" if sym in symbols_data['port'] else "👀 觀察清單"
                        alerts.append(f"**{sym}** ({status})\n└ 📅 財報日: `{e_date}` (倒數 **{days_left}** 天)")

            user = await self.bot.fetch_user(uid)
            if user:
                if alerts:
                    embed = discord.Embed(title="🚨 【盤前財報季雷達預警】", description="\n\n".join(alerts), color=discord.Color.red())
                else:
                    scanned_list = "、".join([f"`{s}`" for s in sorted(combined_symbols)])
                    embed = discord.Embed(title="✅ 【盤前財報季雷達掃描完畢】", description=f"已掃描：{scanned_list}\n\n近 3 日內無財報風險，安全過關！", color=discord.Color.green())
                try:
                    await user.send(embed=embed)
                except discord.Forbidden:
                    pass # 使用者關閉了私訊功能

    @pre_market_risk_monitor.before_loop
    async def before_pre_market_risk_monitor(self):
        await self.bot.wait_until_ready()

    @tasks.loop(minutes=30)
    async def dynamic_market_scanner(self):
        """盤中動態巡邏：每 30 分鐘心跳檢查，僅在盤中 (09:45後) 執行掃描"""
        
        # 1. 計算下一次合法的「盤中掃描起點」(開盤 + 15分)
        target_time = market_time.get_next_market_target_time(reference="open", offset_minutes=15)
        
        # 🔥 2. 推播通知邏輯：如果是「新的」目標時間，就發送通知並記錄下來
        if target_time and target_time != self.last_notified_target:
            await self._notify_next_schedule("盤中動態掃描", target_time)
            self.last_notified_target = target_time  # 更新記憶，確保同一個日子只會通知一次

        # 3. 狀態檢查：如果現在美股未開盤（含週末、國定假日、盤前盤後），直接略過
        if not market_time.is_market_open():
            return
                
        # 4. 避開開盤初期的「造市商無報價期」(09:30 - 09:59)
        # 確保在美東時間 10:00 之後，流動性最充沛時才開始掃描
        now_ny = datetime.now(market_time.ny_tz)
        if now_ny.hour == 9:
            return

        # 5. 執行核心掃描邏輯 (傳入 is_auto=True 讓系統套用 4 小時推播冷卻機制)
        logger.info("🕒 [盤中掃描] 美股交易時段內，啟動動態雷達...")
        await self._run_market_scan_logic(is_auto=True)

    @dynamic_market_scanner.before_loop
    async def before_dynamic_market_scanner(self):
        """確保機器人完全啟動後才開始執行迴圈"""
        await self.bot.wait_until_ready()
        logger.info("盤中動態巡邏機已掛載，將每 30 分鐘偵測一次開盤狀態。")

    @app_commands.command(name="force_scan", description="[Admin] 立即手動執行全站掃描 (不論開盤時間)")
    async def force_scan(self, interaction: discord.Interaction):
        logger.info(f"Admin {interaction.user.name} ({interaction.user.id}) triggered force_scan")
        await interaction.response.send_message("🚀 強制啟動全站掃描中...", ephemeral=True)
        # 用非同步背景執行，避免卡住指令回應
        asyncio.create_task(self._run_market_scan_logic(is_auto=False, triggered_by=interaction.user))

    async def _run_market_scan_logic(self, is_auto=True, triggered_by=None):
        """共用的掃描核心邏輯"""
        try:
            all_watchlists = database.get_all_watchlist() # [(user_id, symbol), ...]
            
            if not all_watchlists:
                if not is_auto and triggered_by:
                     await triggered_by.send("⚠️ **全站觀察清單為空，無法執行掃描。**")
                return

            # 1. 提取所有不重複的標的進行掃描
            unique_symbols = set(sym for uid, sym in all_watchlists)
            scan_results = {}
            
            # 如果是手動觸發，傳送開始訊息
            if not is_auto and triggered_by:
                await triggered_by.send(f"🔍 **開始掃描 {len(unique_symbols)} 檔標的...**")
            
            for sym in unique_symbols:
                try:
                    res = await asyncio.to_thread(market_math.analyze_symbol, sym)
                    if res: scan_results[sym] = res
                except Exception as e:
                    logger.error(f"Error scanning {sym}: {e}")
                await asyncio.sleep(0.5)

            # 若無任何結果且為手動觸發
            if not scan_results:
                if not is_auto and triggered_by:
                    await triggered_by.send("📭 **本次掃描未發現符合策略的交易機會。**")
                return

            # 2. 根據使用者的訂閱清單分發結果
            user_alerts = {}
            for uid, sym in all_watchlists:
                if sym in scan_results:
                    user_alerts.setdefault(uid, []).append(scan_results[sym])

            now = datetime.now(ny_tz)
            # 3. 發送私訊
            for uid, alerts in user_alerts.items():
                user = await self.bot.fetch_user(uid)
                if user:
                    try:
                        # 讀取該名使用者的專屬資金
                        user_capital = database.get_user_capital(uid)

                        # 取得或初始化該使用者的冷卻紀錄字典
                        user_cooldowns = self.signal_cooldowns.setdefault(uid, {})

                        # 用來存放「通過冷卻檢查」的最終發送清單
                        valid_alerts = []

                        for data in alerts:
                            sym = data['symbol']
                        
                            # 🛡️ 冷卻防護判定：只有「自動排程 (is_auto=True)」才需要檢查冷卻
                            if is_auto:
                                last_sent_time = user_cooldowns.get(sym)
                                if last_sent_time:
                                    # 計算距離上次發送過了幾秒
                                    time_diff = (now - last_sent_time).total_seconds()
                                    # 如果時間差小於設定的冷卻秒數 (4小時 * 3600秒)
                                    if time_diff < (self.COOLDOWN_HOURS * 3600):
                                        logger.info(f"[{sym}] 處於 {self.COOLDOWN_HOURS} 小時冷卻期內，略過重複推播。")
                                        continue  # 觸發冷卻！直接跳過這個標的，不加入 valid_alerts
                            # 通過冷卻檢查 (或是手動強制掃描 is_auto=False)，加入發送清單
                            valid_alerts.append(data)

                            # 🔄 更新大腦記憶：只有自動排程才更新冷卻時間
                            # (這樣設計是為了避免您手動 /force_scan 時，意外重置了原本的冷卻計時器)
                            if is_auto:
                                user_cooldowns[sym] = now

                        # 只有當 valid_alerts 裡面有東西時，才真正呼叫 Discord API 發送訊息
                        if valid_alerts:
                            try:
                                title = "📡 **【盤中動態掃描】發現建倉機會：**" if is_auto else "⚡ **【管理員強制掃描】雷達結果：**"
                                await user.send(title)
                                for data in valid_alerts:
                                    await user.send(embed=create_scan_embed(data, user_capital))
                            except Exception as e:
                                logger.error(f"無法發送私訊給 User ID {uid}: {e}")
                    except discord.Forbidden:
                        pass  # 使用者關閉了私訊功能
        except Exception as e:
            logger.error(f"掃描邏輯執行錯誤: {e}")

    @tasks.loop(count=1)
    async def dynamic_after_market_report(self):
        """16:15：持倉結算與防禦建議 (依使用者分發私訊)"""
        logger.info("Starting dynamic_after_market_report task.")
        target_time = market_time.get_next_market_target_time(reference="close", offset_minutes=15)
        await self._notify_next_schedule("盤後結算報告", target_time)
        await asyncio.sleep(market_time.get_sleep_seconds(target_time))

        all_portfolios = database.get_all_portfolio()
        if not all_portfolios: return
        
        # 1. 將全站持倉依 user_id 分群
        user_ports = {}
        for row in all_portfolios:
            uid = row[0]
            # row[2:] 取出 (symbol, opt_type, strike, expiry, entry_price, quantity)
            user_ports.setdefault(uid, []).append(row[2:])

        # 2. 分別計算損益並發送私訊
        for uid, rows in user_ports.items():
            user_capital = database.get_user_capital(uid)

            # 將資金參數傳遞給重構後的結算引擎
            report_lines = await asyncio.to_thread(market_analysis.portfolio.check_portfolio_status_logic, rows, user_capital)            
            if report_lines:
                user = await self.bot.fetch_user(uid)
                if user:
                    embed = discord.Embed(title="📝 您的選擇權持倉健檢", description="\n".join(report_lines), color=discord.Color.gold())
                    try:
                        await user.send("📊 **【盤後結算報告：部位損益與建議】**", embed=embed)
                    except discord.Forbidden:
                        pass

    @dynamic_after_market_report.before_loop
    async def before_dynamic_after_market_report(self):
        await self.bot.wait_until_ready()

    async def _notify_next_schedule(self, task_name, target_time):
        """通知所有使用者下一次任務執行時間"""
        if not target_time:
            return
        
        # 使用 Discord Timestamp 讓時間自動轉換為使用者當地時區
        unix_ts = int(target_time.timestamp())
        msg = f"📅 **{task_name}** 下次執行時間: <t:{unix_ts}:F> (<t:{unix_ts}:R>)"
        try:
            await self.bot.notify_all_users(msg)
        except Exception as e:
            logger.warning(f"Failed to send schedule notification: {e}")

async def setup(bot):
    await bot.add_cog(SchedulerCog(bot))