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
from services import market_data_service
from services import news_service, llm_service, reddit_service

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
        
        # 財報風險預警天數設定
        self.EARNINGS_WARNING_DAYS = 14

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
            e_date = await asyncio.to_thread(market_math.get_next_earnings_date, sym)
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
                    if 0 <= days_left <= self.EARNINGS_WARNING_DAYS:
                        status = "⚠️ **持倉高風險**" if sym in symbols_data['port'] else "👀 觀察清單"
                        alerts.append(f"**{sym}** ({status})\n└ 📅 財報日: `{e_date}` (倒數 **{days_left}** 天)")

            user = await self.bot.fetch_user(uid)
            if user:
                if alerts:
                    embed = discord.Embed(title="🚨 【盤前財報季雷達預警】", description="\n\n".join(alerts), color=discord.Color.red())
                else:
                    scanned_list = "、".join([f"`{s}`" for s in sorted(combined_symbols)])
                    embed = discord.Embed(title="✅ 【盤前財報季雷達掃描完畢】", description=f"已掃描：{scanned_list}\n\n近 {self.EARNINGS_WARNING_DAYS} 日內無財報風險，安全過關！", color=discord.Color.green())
                try:
                    await self.bot.queue_dm(uid, embed=embed)
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
            all_watchlists = database.get_all_watchlist() # [(user_id, symbol, stock_cost, use_llm), ...]
            
            if not all_watchlists:
                if not is_auto and triggered_by:
                     await triggered_by.send("⚠️ **全站觀察清單為空，無法執行掃描。**")
                return

            # 1. 提取所有不重複的標的與成本對進行掃描
            unique_targets = set((sym, stock_cost, use_llm) for uid, sym, stock_cost, use_llm in all_watchlists)
            scan_results = {}
            news_cache = {} # 單次掃描內的新聞快取
            reddit_cache = {} # 單次掃描內的 Reddit 討論快取
            
            # 如果是手動觸發，傳送開始訊息
            if not is_auto and triggered_by:
                unique_symbols = set(sym for sym, _, _ in unique_targets)
                await triggered_by.send(f"🔍 **開始掃描 {len(unique_symbols)} 檔標的...**")
            
            for sym, stock_cost, use_llm in unique_targets:
                trigger_name = f"User {triggered_by.id}" if triggered_by else "System Auto"
                logger.info(f"{trigger_name} scanning {sym} (Cost: {stock_cost}, LLM: {use_llm})")
                try:
                    res = await asyncio.to_thread(market_math.analyze_symbol, sym, stock_cost)
                    if res:
                        # 優先從快取取得新聞
                        if sym not in news_cache:
                            news_cache[sym] = await news_service.fetch_recent_news(sym)
                        
                        # 優先從快取取得 Reddit 討論
                        if sym not in reddit_cache:
                            reddit_cache[sym] = await reddit_service.get_reddit_context(sym)
                        
                        news_text = news_cache[sym]
                        reddit_text = reddit_cache[sym]
                        
                        if use_llm:
                            ai_verdict = await llm_service.evaluate_trade_risk(sym, res['strategy'], news_text, reddit_text)
                            res['ai_decision'] = ai_verdict.get('decision', 'APPROVE')
                            res['ai_reasoning'] = ai_verdict.get('reasoning', '無資料')
                        else:
                            res['ai_decision'] = 'SKIP'
                            res['ai_reasoning'] = '未啟用 LLM 語意風控'
                        res['news_text'] = news_text
                        res['reddit_text'] = reddit_text
                        scan_results[(sym, stock_cost, use_llm)] = res
                except Exception as e:
                    logger.error(f"Error scanning {sym} with cost {stock_cost}: {e}")
                await asyncio.sleep(0.5)

            # 若無任何結果且為手動觸發
            if not scan_results:
                if not is_auto and triggered_by:
                    await triggered_by.send("📭 **本次掃描未發現符合策略的交易機會。**")
                return

            # 2. 根據使用者的訂閱清單分發結果
            user_alerts = {}
            for uid, sym, stock_cost, use_llm in all_watchlists:
                if (sym, stock_cost, use_llm) in scan_results:
                    user_alerts.setdefault(uid, []).append(scan_results[(sym, stock_cost, use_llm)])

            now = datetime.now(ny_tz)
            # 3. 發送私訊 (整合 NRO 風控引擎)
            from market_analysis.portfolio import optimize_position_risk

            # 🚀 效能優化：在分發前先透過 Finnhub 抓一次基準 SPY 價格
            try:
                spy_quote = market_data_service.get_quote("SPY")
                spy_price = spy_quote.get('c', 500.0) if spy_quote else 500.0
            except:
                spy_price = 500.0

            for uid, alerts in user_alerts.items():
                user = await self.bot.fetch_user(uid)
                if not user:
                    continue

                try:
                    # A. 取得該使用者的資金與現有曝險狀況
                    user_capital = database.get_user_capital(uid) or 50000.0
                    current_stats = database.get_user_portfolio_stats(uid)
                    current_total_delta = current_stats.get('total_weighted_delta', 0.0)

                    user_cooldowns = self.signal_cooldowns.setdefault(uid, {})
                    valid_alerts = []

                    for data in alerts:
                        sym = data['symbol']
                        
                        # B. 冷卻檢查 (維持原樣)
                        if is_auto:
                            last_sent_time = user_cooldowns.get(sym)
                            if last_sent_time:
                                time_diff = (now - last_sent_time).total_seconds()
                                if time_diff < (self.COOLDOWN_HOURS * 3600):
                                    continue 
                        
                        # 🚀 C. 核心整合：針對該使用者進行 NRO 運算
                        strategy = data.get('strategy', '')
                        unit_weighted_delta = data.get('weighted_delta', 0.0)
                        
                        # 1. 計算安全口數與對沖建議
                        safe_qty, hedge_spy = optimize_position_risk(
                            current_delta=current_total_delta,
                            unit_weighted_delta=unit_weighted_delta,
                            user_capital=user_capital,
                            spy_price=spy_price,
                            strategy=strategy,
                            risk_limit_pct=15.0
                        )

                        # 2. 計算成交 1 口後的預期總曝險 (What-if)
                        # 使用 side_multiplier 校正部位方向
                        side_multiplier = -1 if "STO" in strategy else 1
                        new_trade_impact = unit_weighted_delta * side_multiplier
                        projected_total_delta = current_total_delta + new_trade_impact
                        projected_exposure_pct = (projected_total_delta * spy_price / user_capital) * 100

                        # 3. 回填 NRO 數據至 data 字典，供 create_scan_embed 使用
                        data.update({
                            'safe_qty': safe_qty,
                            'hedge_spy': hedge_spy,
                            'projected_exposure_pct': projected_exposure_pct,
                            'spy_price': spy_price,
                            'suggested_contracts': data.get('suggested_contracts', 1) # 預設至少1口以供對比
                        })

                        valid_alerts.append(data)
                        if is_auto:
                            user_cooldowns[sym] = now

                    # D. 發送經過風控過濾的 Embed
                    if valid_alerts:
                        title = "📡 **【盤中動態掃描】NRO 風控已介入判定：**" if is_auto else "⚡ **【管理員強制掃描】風險模擬結果：**"
                        await self.bot.queue_dm(uid, message=title)
                        for data in valid_alerts:
                            # 這裡傳入的 data 已經包含了該使用者的客製化風控數據
                            await self.bot.queue_dm(uid, embed=create_scan_embed(data, user_capital))

                except Exception as e:
                    logger.error(f"無法發送私訊或計算風險給 User ID {uid}: {e}")
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
        
        user_ports = {}
        for row in all_portfolios:
            uid = row[0]
            # row[2:] 取出 (symbol, opt_type, strike, expiry, entry_price, quantity, stock_cost)
            user_ports.setdefault(uid, []).append(row[2:])

        from cogs.embed_builder import create_portfolio_report_embed

        for uid, rows in user_ports.items():
            user_capital = database.get_user_capital(uid)

            # 執行重構後的結算引擎 (回傳 list of strings)
            report_lines = await asyncio.to_thread(
                market_analysis.portfolio.check_portfolio_status_logic, 
                rows, 
                user_capital
            )            
            
            if report_lines:
                user = await self.bot.fetch_user(uid)
                if user:
                    embed = create_portfolio_report_embed(report_lines)
                    
                    try:
                        await self.bot.queue_dm(
                            uid, 
                            message="📊 **【Nexus Seeker 盤後結算系統】**", 
                            embed=embed
                        )
                    except discord.Forbidden:
                        logger.warning(f"無法發送私訊給用戶 {uid}，請檢查權限設定。")

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