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

ny_tz = ZoneInfo("America/New_York")
logger = logging.getLogger(__name__)

class TradingCog(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.pre_market_risk_monitor.start()
        self.dynamic_market_scanner.start()
        self.dynamic_after_market_report.start()
        logger.info("TradingCog loaded. Background tasks started.")

    def cog_unload(self):
        self.pre_market_risk_monitor.cancel()
        self.dynamic_market_scanner.cancel()
        self.dynamic_after_market_report.cancel()
        logger.info("TradingCog unloaded. Background tasks cancelled.")

    # ==========================================
    # 持倉 (Portfolio) 管理指令 (綁定 user_id)
    # ==========================================
    @app_commands.command(name="add_trade", description="將新的選擇權部位加入您的專屬監控庫")
    @app_commands.choices(opt_type=[
        app_commands.Choice(name="Put (賣權)", value="put"),
        app_commands.Choice(name="Call (買權)", value="call")
    ])
    async def add_trade(self, interaction: discord.Interaction, symbol: str, opt_type: app_commands.Choice[str], strike: float, expiry: str, entry_price: float, quantity: int):
        symbol = symbol.upper()
        user_id = interaction.user.id
        try:
            trade_id = database.add_portfolio_record(user_id, symbol, opt_type.value, strike, expiry, entry_price, quantity)
            action_text = "賣出 (STO)" if quantity < 0 else "買入 (BTO)"
            # 私訊回覆使用者
            await interaction.response.send_message(
                f"✅ **新增成功 (ID: {trade_id})**: {action_text} {abs(quantity)} 口 `{symbol}` ${strike} {opt_type.value.upper()} ({expiry} 到期)", 
                ephemeral=True
            )
        except Exception as e:
            await interaction.response.send_message(f"❌ 寫入失敗: {e}", ephemeral=True)

    @app_commands.command(name="set_capital", description="設定您的總資金規模，用於精算專屬的凱利建議倉位")
    async def set_capital(self, interaction: discord.Interaction, capital: float):
        if capital <= 0:
            await interaction.response.send_message("❌ 資金必須大於 0。", ephemeral=True)
            return
        user_id = interaction.user.id
        database.set_user_capital(user_id, capital)
        await interaction.response.send_message(f"💰 已將您的專屬總資金設定為 `${capital:,.2f}`", ephemeral=True)

    @app_commands.command(name="list_trades", description="列出您目前資料庫中的所有持倉")
    async def list_trades(self, interaction: discord.Interaction):
        user_id = interaction.user.id
        rows = database.get_user_portfolio(user_id)
        if not rows:
            await interaction.response.send_message("📭 您目前無持倉紀錄。", ephemeral=True)
            return
        msg = "📊 **【您的專屬持倉清單】**\n"
        for row in rows:
            trade_id, sym, o_type, strike, exp, price, qty = row
            action = "賣出 (STO)" if qty < 0 else "買入 (BTO)"
            msg += f"`ID:{trade_id:02d}` | **{sym}** | {exp} 到期 | ${strike} {o_type.upper()} | {action} {abs(qty)}口 | 成本: ${price}\n"
        await interaction.response.send_message(msg, ephemeral=True)

    @app_commands.command(name="remove_trade", description="將部位從您的監控庫中移除")
    async def remove_trade(self, interaction: discord.Interaction, trade_id: int):
        user_id = interaction.user.id
        record = database.delete_portfolio_record(user_id, trade_id)
        if record:
            await interaction.response.send_message(f"🗑️ **已刪除紀錄 (ID: {trade_id})**: `{record[0]}` ${record[1]} {record[2].upper()} 已移除。", ephemeral=True)
        else:
            await interaction.response.send_message(f"❌ 找不到屬於您的 ID `{trade_id}`。", ephemeral=True)

    # ==========================================
    # 觀察清單 (Watchlist) 管理指令 (綁定 user_id)
    # ==========================================
    @app_commands.command(name="add_watch", description="將股票代號加入您的雷達掃描清單")
    async def add_watch(self, interaction: discord.Interaction, symbol: str):
        symbol = symbol.upper()
        user_id = interaction.user.id
        success = database.add_watchlist_symbol(user_id, symbol)
        if success:
            await interaction.response.send_message(f"👁️ 已將 `{symbol}` 加入您的觀察清單！開盤將自動私訊精算結果。", ephemeral=True)
        else:
            await interaction.response.send_message(f"⚠️ `{symbol}` 已經在您的觀察清單中了。", ephemeral=True)

    @app_commands.command(name="list_watch", description="列出您的雷達觀察清單")
    async def list_watch(self, interaction: discord.Interaction):
        user_id = interaction.user.id
        symbols = database.get_user_watchlist(user_id)
        if not symbols:
            await interaction.response.send_message("📭 您的觀察清單是空的。", ephemeral=True)
            return
        msg = "📡 **【您的專屬觀察清單】**\n" + "、".join([f"`{sym}`" for sym in symbols])
        await interaction.response.send_message(msg, ephemeral=True)

    @app_commands.command(name="remove_watch", description="將股票代號從您的觀察清單移除")
    async def remove_watch(self, interaction: discord.Interaction, symbol: str):
        symbol = symbol.upper()
        user_id = interaction.user.id
        if database.delete_watchlist_symbol(user_id, symbol):
            await interaction.response.send_message(f"🗑️ 已將 `{symbol}` 從您的觀察清單移除。", ephemeral=True)
        else:
            await interaction.response.send_message(f"❌ 找不到 `{symbol}`。", ephemeral=True)

    @app_commands.command(name="scan", description="手動對特定股票執行 Delta 中性掃描")
    async def manual_scan(self, interaction: discord.Interaction, symbol: str):
        logger.info(f"User {interaction.user.id} triggered manual_scan for {symbol}")
        await interaction.response.defer(ephemeral=True)
        result = await asyncio.to_thread(market_math.analyze_symbol, symbol.upper())
        if result:
            # 🔥 讀取該名使用者的專屬資金
            user_capital = database.get_user_capital(interaction.user.id)
            embed = self._create_embed(result, user_capital)
            await interaction.followup.send(embed=embed)
        else:
            await interaction.followup.send(f"📊 目前 `{symbol.upper()}` 無明確訊號或無合適合約。")

    # ==========================================
    # 動態排程任務 (私訊分發引擎)
    # ==========================================
    @tasks.loop()
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
                    if 0 <= days_left <= 3:
                        status = "⚠️ **持倉高風險**" if sym in symbols_data['port'] else "👀 觀察清單"
                        alerts.append(f"**{sym}** ({status})\n└ 📅 財報日: `{e_date}` (倒數 **{days_left}** 天)")

            if alerts:
                user = await self.bot.fetch_user(uid)
                if user:
                    embed = discord.Embed(title="🚨 【盤前財報季雷達預警】", description="\n\n".join(alerts), color=discord.Color.red())
                    try:
                        await user.send(embed=embed)
                    except discord.Forbidden:
                        pass # 使用者關閉了私訊功能

    @tasks.loop()
    async def dynamic_market_scanner(self):
        """09:45：盤中掃描機會 (依使用者分發私訊)"""
        logger.info("Starting dynamic_market_scanner task.")
        target_time = market_time.get_next_market_target_time(reference="open", offset_minutes=15)
        await self._notify_next_schedule("盤中動態掃描", target_time)
        await asyncio.sleep(market_time.get_sleep_seconds(target_time))
        
        await self._run_market_scan_logic(is_auto=True)

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

            # 3. 發送私訊
            for uid, alerts in user_alerts.items():
                user = await self.bot.fetch_user(uid)
                if user:
                    try:
                        # 🔥 讀取該名使用者的專屬資金
                        user_capital = database.get_user_capital(uid)
                        
                        if is_auto:
                            header = "🕒 **美股已開盤 15 分鐘，為您精算出以下機會：**"
                        else:
                            trigger_name = triggered_by.display_name if triggered_by else "Admin"
                            header = f"🔧 **管理員 {trigger_name} 手動觸發了即時掃描：**"

                        await user.send(header)
                        for data in alerts:
                            await user.send(embed=self._create_embed(data, user_capital))
                    except discord.Forbidden:
                        pass
                        
            # 手動觸發完成通知
            if not is_auto and triggered_by:
                await triggered_by.send("✅ **掃描與分發完成。**")

        except Exception as e:
            if not is_auto and triggered_by:
                await triggered_by.send(f"❌ **掃描執行發生錯誤**: {str(e)}")
            raise e

    @tasks.loop()
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
            # row[1:] 取出 (trade_id, symbol, opt_type, strike, expiry, entry_price, quantity)
            user_ports.setdefault(uid, []).append(row[1:])

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

    def _create_embed(self, data, user_capital=100000.0):
        colors = {"STO_PUT": discord.Color.green(), "STO_CALL": discord.Color.red(), "BTO_CALL": discord.Color.blue(), "BTO_PUT": discord.Color.orange()}
        titles = {"STO_PUT": "🟢 Sell To Open Put", "STO_CALL": "🔴 Sell To Open Call", "BTO_CALL": "🚀 Buy To Open Call", "BTO_PUT": "⚠️ Buy To Open Put"}
        embed = discord.Embed(title=f"{titles[data['strategy']]} - {data['symbol']}", color=colors.get(data['strategy'], discord.Color.default()))
        
        # 展示標的現價
        embed.add_field(name="標的現價", value=f"${data['price']:.2f}")
        
        # 展示 RSI/20MA
        embed.add_field(name="RSI/20MA", value=f"{data['rsi']:.2f} / ${data['sma20']:.2f}")
        
        # 展示 HVR (波動率位階)
        hvr_status = "🔥 高" if data['hv_rank'] >= 50 else ("⚡ 中" if data['hv_rank'] >= 30 else "🧊 低")
        embed.add_field(name="HV Rank (波動率位階)", value=f"`{data['hv_rank']:.1f}%` {hvr_status}")

        # 展示 VRP (波動率風險溢酬)
        vrp_pct = data.get('vrp', 0.0) * 100
        # 賣方需要正溢酬，買方反而偏好負溢酬(買入便宜的波動率)
        if "STO" in data['strategy']:
            vrp_icon = "✅ 溢價 (具備數學優勢)" if vrp_pct > 0 else "⚠️ 折價 (期望值為負)"
        else:
            vrp_icon = "✅ 折價 (買方成本低估)" if vrp_pct < 0 else "⚠️ 溢價 (買方成本過高)"
        embed.add_field(name="VRP (波動率風險溢酬)", value=f"`{vrp_pct:+.2f}%` {vrp_icon}")

        # 展示 IV 期限結構 (Term Structure)
        ts_ratio_str = f"`{data['ts_ratio']:.2f}`"
        # 若發生倒掛，給予強烈視覺提示
        if data['ts_ratio'] >= 1.05:
            ts_ratio_str = f"**{ts_ratio_str}** {data['ts_state']} 🎯"
        else:
            ts_ratio_str = f"{ts_ratio_str} {data['ts_state']}"
        embed.add_field(name="IV 期限結構 (30D/60D)", value=ts_ratio_str)

        # 展示垂直波動率偏態 (Vertical Skew)
        v_skew_str = f"`{data['v_skew']:.2f}` {data.get('v_skew_state', '')}"
        if data.get('v_skew') >= 1.30:
            v_skew_str = f"**{data['v_skew']:.2f}** {data.get('v_skew_state', '')}"
        embed.add_field(name="垂直偏態 (Put/Call IV Ratio)", value=v_skew_str)
        
        # 展示 AROC (年化報酬率)
        if "STO" in data['strategy']:
            embed.add_field(name="AROC (年化報酬率)", value=f"`{data['aroc']:.1f}%` 💰")

            # 凱利準則部位建議
            alloc_pct = data.get('alloc_pct', 0.0)
            margin_per_contract = data.get('margin_per_contract', 0.0)
            suggested_contracts = 0

            if alloc_pct > 0 and margin_per_contract > 0:
                allocated_capital = user_capital * alloc_pct
                suggested_contracts = int(allocated_capital // margin_per_contract)
                
            if suggested_contracts > 0:
                embed.add_field(name="⚖️ 凱利準則建議倉位", value=f"`{suggested_contracts} 口` (佔總資金 {alloc_pct*100:.1f}%)")
            else:
                embed.add_field(name="⚖️ 凱利準則建議倉位", value=f"`本金門檻不足` (建議佔比 {alloc_pct*100:.1f}%)")

        # 🔥 新增這區塊：財報預期波動與雷區判定
        if 0 <= data.get('earnings_days', -1) <= 14:
            mmm_str = f"±{data['mmm_pct']:.1f}% (倒數 {data['earnings_days']} 天)"
            bounds_str = f"下緣 ${data['safe_lower']:.2f} / 上緣 ${data['safe_upper']:.2f}"
            
            # 判斷系統挑選的履約價 (strike) 是否落在安全帶之外
            strike = data['strike']
            strategy = data['strategy']
            is_safe = False
            if strategy == "STO_PUT" and strike <= data['safe_lower']:
                is_safe = True
            elif strategy == "STO_CALL" and strike >= data['safe_upper']:
                is_safe = True
                
            safety_icon = "✅ 避開雷區 (適宜收租)" if is_safe else "💣 位於雷區 (高風險)"
            embed.add_field(name="📊 財報預期波動 (MMM)", value=f"`{mmm_str}`\n{bounds_str}\n{safety_icon}", inline=False)
            
        embed.add_field(name="精算合約", value=f"{data['target_date']} (${data['strike']})", inline=False)

        # 預期波動區間 (Expected Move) 與 損益兩平防線
        em = data.get('expected_move', 0.0)
        em_lower = data.get('em_lower', 0.0)
        em_upper = data.get('em_upper', 0.0)
        
        if "STO_PUT" in data['strategy']:
            breakeven = data['strike'] - data['bid']
            em_info = f"1σ 預期下緣: `${em_lower:.2f}` (預期最大跌幅 -${em:.2f})\n" \
                      f"🛡️ 損益兩平點: **`${breakeven:.2f}`**\n" \
                      f"✅ 防線已建構於預期暴跌區間外"
            embed.add_field(name="🎯 機率圓錐 (1σ 預期波動)", value=em_info, inline=False)
            
        elif "STO_CALL" in data['strategy']:
            breakeven = data['strike'] + data['bid']
            em_info = f"1σ 預期上緣: `${em_upper:.2f}` (預期最大漲幅 +${em:.2f})\n" \
                      f"🛡️ 損益兩平點: **`${breakeven:.2f}`**\n" \
                      f"✅ 防線已建構於預期暴漲區間外"
            embed.add_field(name="🎯 機率圓錐 (1σ 預期波動)", value=em_info, inline=False)

        # 報價與流動性分析 (Bid/Ask & Spread)
        spread_info = f"`Bid ${data['bid']:.2f}` / `Ask ${data['ask']:.2f}`\n" \
                      f"└ 價差: `${data['spread']:.2f}` ({data['spread_ratio']:.1f}%)"
        # 如果雖然通過濾網，但流動性處於邊緣地帶，給予黃色警告
        if data['spread'] > 0.15 and data['spread_ratio'] > 8.0:
            spread_info += " ⚠️ 流動性偏低，建議掛限價單 (Limit Order)"
        else:
            spread_info += " 💧 流動性充沛"
        embed.add_field(name="報價與流動性分析", value=spread_info, inline=False)

        embed.add_field(name="Delta / 當前合約 IV", value=f"{data['delta']:.3f} / {data['iv']:.1%}")
        
        return embed

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
    await bot.add_cog(TradingCog(bot))