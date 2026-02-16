import discord
from discord.ext import tasks, commands
from discord import app_commands
import asyncio
from datetime import datetime
from zoneinfo import ZoneInfo

import database
import market_math
import market_time

ny_tz = ZoneInfo("America/New_York")

class TradingCog(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.pre_market_risk_monitor.start()
        self.dynamic_market_scanner.start()
        self.dynamic_after_market_report.start()

    def cog_unload(self):
        self.pre_market_risk_monitor.cancel()
        self.dynamic_market_scanner.cancel()
        self.dynamic_after_market_report.cancel()

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
        await interaction.response.defer(ephemeral=True) # 隱藏掃描結果，只讓指令發送者看到
        result = await asyncio.to_thread(market_math.analyze_symbol, symbol.upper())
        if result:
            embed = self._create_embed(result)
            await interaction.followup.send(embed=embed)
        else:
            await interaction.followup.send(f"📊 目前 `{symbol.upper()}` 無明確訊號或無合適合約。")

    # ==========================================
    # 動態排程任務 (私訊分發引擎)
    # ==========================================
    @tasks.loop()
    async def pre_market_risk_monitor(self):
        """09:00：盤前財報警報 (依使用者分發私訊)"""
        target_time = market_time.get_next_market_target_time(reference="open", offset_minutes=-30)
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
        target_time = market_time.get_next_market_target_time(reference="open", offset_minutes=15)
        await asyncio.sleep(market_time.get_sleep_seconds(target_time))
        
        all_watchlists = database.get_all_watchlist() # [(user_id, symbol), ...]
        if not all_watchlists: return

        # 1. 提取所有不重複的標的進行掃描
        unique_symbols = set(sym for uid, sym in all_watchlists)
        scan_results = {}
        
        for sym in unique_symbols:
            res = await asyncio.to_thread(market_math.analyze_symbol, sym)
            if res: scan_results[sym] = res
            await asyncio.sleep(0.5)

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
                    await user.send(f"🕒 **美股已開盤 15 分鐘，為您精算出以下機會：**")
                    for data in alerts:
                        await user.send(embed=self._create_embed(data))
                except discord.Forbidden:
                    pass

    @tasks.loop()
    async def dynamic_after_market_report(self):
        """16:15：持倉結算與防禦建議 (依使用者分發私訊)"""
        target_time = market_time.get_next_market_target_time(reference="close", offset_minutes=15)
        await asyncio.sleep(market_time.get_sleep_seconds(target_time))

        all_portfolios = database.get_all_portfolio()
        if not all_portfolios: return
        
        # 1. 將全站持倉依 user_id 分群
        user_ports = {}
        for row in all_portfolios:
            uid = row[0]
            # row[1:] 取出 (trade_id, symbol, opt_type, strike, expiry, entry_price, quantity)
            # 這樣 market_math.py 就完全不需要修改，無縫接軌！
            user_ports.setdefault(uid, []).append(row[1:])

        # 2. 分別計算損益並發送私訊
        for uid, rows in user_ports.items():
            report_lines = await asyncio.to_thread(market_math.check_portfolio_status_logic, rows)
            if report_lines:
                user = await self.bot.fetch_user(uid)
                if user:
                    embed = discord.Embed(title="📝 您的選擇權持倉健檢", description="\n".join(report_lines), color=discord.Color.gold())
                    try:
                        await user.send("📊 **【盤後結算報告：部位損益與建議】**", embed=embed)
                    except discord.Forbidden:
                        pass

    def _create_embed(self, data):
        colors = {"STO_PUT": discord.Color.green(), "STO_CALL": discord.Color.red(), "BTO_CALL": discord.Color.blue(), "BTO_PUT": discord.Color.orange()}
        titles = {"STO_PUT": "🟢 Sell To Open Put", "STO_CALL": "🔴 Sell To Open Call", "BTO_CALL": "🚀 Buy To Open Call", "BTO_PUT": "⚠️ Buy To Open Put"}
        embed = discord.Embed(title=f"{titles[data['strategy']]} - {data['symbol']}", color=colors.get(data['strategy'], discord.Color.default()))
        embed.add_field(name="現價", value=f"${data['price']:.2f}")
        embed.add_field(name="RSI/20MA", value=f"{data['rsi']:.2f} / ${data['sma20']:.2f}")
        embed.add_field(name="合約", value=f"{data['target_date']} (${data['strike']})", inline=False)
        embed.add_field(name="報價", value=f"${data['bid']} / ${data['ask']}")
        embed.add_field(name="精算 Delta", value=f"{data['delta']:.3f}")
        return embed

async def setup(bot):
    await bot.add_cog(TradingCog(bot))