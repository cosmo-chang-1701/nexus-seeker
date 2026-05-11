import discord
from discord.ext import commands, tasks
from discord import app_commands
import logging
from datetime import datetime
from zoneinfo import ZoneInfo

from services.calendar_service import calendar_service
from market_analysis.volatility_inspector import VolatilityInspector
import database

logger = logging.getLogger(__name__)
ny_tz = ZoneInfo("America/New_York")


class CalendarCog(commands.Cog):
    """
    [Calendar] Calendar-Aware Risk & Volatility Guard.
    Monitors high-impact events and provides proactive alerts.
    """

    def __init__(self, bot):
        self.bot = bot
        self.vol_inspector = VolatilityInspector(bot)
        from services.event_monitor import EventMonitor

        self.monitor = EventMonitor(bot)
        self.event_checker.start()

    def cog_unload(self):
        self.event_checker.cancel()

    @tasks.loop(hours=4)
    async def event_checker(self):
        """NYSE Dynamic Scheduler heartbeat for events."""
        logger.info("🕒 [EventMonitor] 執行重大事件週期檢查...")
        await self.monitor.check_upcoming_events()

    @event_checker.before_loop
    async def before_event_checker(self):
        await self.bot.wait_until_ready()

    @app_commands.command(
        name="calendar", description="顯示影響目前投資組合的即時重大事件"
    )
    async def calendar(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        user_id = interaction.user.id

        events = await calendar_service.get_portfolio_events(user_id)

        if not events:
            return await interaction.followup.send(
                "📭 未來 7 日內無影響持倉標的的重大事件或財報。"
            )

        embed = discord.Embed(
            title="📅 【 重大市場事件 & 財報日曆 】",
            description="針對您的持倉標的過濾後的高影響力事件：",
            color=discord.Color.blue(),
            timestamp=datetime.now(),
        )

        for event in events[:20]:  # Limit to 20 for embed safety
            if event["type"] == "ECONOMIC":
                impact_color = "🔴" if event["impact"].lower() == "high" else "🟡"
                field_name = f"{impact_color} {event['event']} ({event['country']})"
                field_value = (
                    f"⏰ TTE: `{event['tte_hours']}` 小時 | 時間: `{event['time']}`"
                )
            else:
                field_name = f"📊 {event['symbol']} 財報發布"
                field_value = (
                    f"⏰ TTE: `{event['tte_hours']}` 小時 | 日期: `{event['date']}`"
                )

            embed.add_field(name=field_name, value=field_value, inline=False)

        embed.set_footer(text="Calendar-Aware Guard | Nexus Seeker")
        await interaction.followup.send(embed=embed)

    @app_commands.command(
        name="iv_rank", description="掃描觀察清單中具備高 IV Rank 或財報前夕的標的"
    )
    async def iv_rank(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        all_watchlists = database.get_all_watchlist()
        user_id = interaction.user.id
        user_watch = [row[1] for row in all_watchlists if row[0] == user_id]

        if not user_watch:
            return await interaction.followup.send(
                "📭 觀察清單為空，無法執行 IV 掃描。"
            )

        results = await self.vol_inspector.run_scan(user_watch, user_id)

        # Filter for IV Rank > 80 or high risk vol
        high_iv_results = [
            r for r in results if r.get("iv_rank", 0) > 80 or r.get("is_high_risk_vol")
        ]

        if not high_iv_results:
            return await interaction.followup.send(
                "🔎 掃描完成，未發現 IV Rank > 80% 的高波動標的。"
            )

        embed = discord.Embed(
            title="🔥 【 高波動 & IV Crush 風險掃描 】", color=discord.Color.red()
        )

        for res in high_iv_results:
            risk_label = "🚨 CRITICAL" if res["is_high_risk_vol"] else "⚠️ HIGH IV"
            field_name = f"[{risk_label}] {res['symbol']} (IVR: {res['iv_rank']}%)"
            field_value = (
                f"價格: `${res['price']}` | IV: `{res['iv_current']}%` | HV: `{res['hv_current']}%` \n"
                f"財報 TTE: `{res['tte_hours']:.1f}` 小時 \n"
                f"**策略建議**: {res['strategy']} \n"
                f"**邏輯**: {res['trigger_logic']}"
            )
            embed.add_field(name=field_name, value=field_value, inline=False)

        embed.set_footer(text="IV Rank Scanner | Nexus Seeker")
        await interaction.followup.send(embed=embed)

    @app_commands.command(
        name="event_impact",
        description="針對特定即時事件進行 Greeks (Delta, Vega) 的 What-if 模擬",
    )
    @app_commands.describe(
        symbol="欲模擬的標的代號", vol_move="預期波動率變動 % (例如 20 代表 20%)"
    )
    async def event_impact(
        self, interaction: discord.Interaction, symbol: str, vol_move: float = 20.0
    ):
        await interaction.response.defer(ephemeral=True)
        symbol = symbol.upper()
        user_id = interaction.user.id

        # Get Greeks for the user's positions in this symbol
        from database.portfolio import get_user_portfolio

        portfolio = get_user_portfolio(user_id)
        symbol_positions = [p for p in portfolio if p[1] == symbol]

        if not symbol_positions:
            return await interaction.followup.send(
                f"📭 您目前並未持有 `{symbol}` 的部位，無法進行模擬。"
            )

        total_delta = sum(p[8] for p in symbol_positions)
        # We might not have Vanna stored yet, need to calculate it
        # For simplicity in this CLI, we simulate impact based on Delta and assumed Vanna relation

        # Calculate Vanna for each position
        from market_analysis.greeks import calculate_vanna
        from services import market_data_service

        price_data = await market_data_service.get_quote(symbol)
        price = price_data.get("c", 0.0)

        total_vanna = 0.0
        for p in symbol_positions:
            # (asset_id, sym, opt_type, strike, expiry, entry_price, quantity, stock_cost, delta, theta, gamma, category)
            qty = p[6]
            opt_type = "c" if p[2].lower() == "call" else "p"
            strike = p[3]
            expiry = p[4]

            # Calculate years to expiry
            t_dt = datetime.strptime(expiry, "%Y-%m-%d")
            t_years = (t_dt - datetime.now()).days / 365.0

            # Get current IV from market data or use placeholder
            # Real implementation would fetch from chain
            vanna = calculate_vanna(opt_type, price, strike, t_years, 0.3, 0.0)
            total_vanna += vanna * qty * 100  # Multiplier for contract size

        from market_analysis.risk_engine import calculate_vega_adjusted_delta

        # Use vanna_weight adjustment for upcoming event simulation
        vol_change_decimal = vol_move / 100.0
        adj_delta = calculate_vega_adjusted_delta(
            total_delta, total_vanna, vol_change_decimal, event_multiplier=1.8
        )

        delta_shift = adj_delta - total_delta

        embed = discord.Embed(
            title=f"🎲 【 {symbol} 事件風險模擬 (What-if) 】",
            description=f"假設波動率變動 `{vol_move}%` 時，部位 Greeks 的動態偏移：",
            color=discord.Color.gold(),
        )

        embed.add_field(
            name="目前 Beta-Weighted Delta", value=f"`{total_delta:.2f}`", inline=True
        )
        embed.add_field(
            name="目前 Vanna (曝險變化率)", value=f"`{total_vanna:.2f}`", inline=True
        )
        embed.add_field(
            name="預期 Hidden Delta", value=f"`{adj_delta:.2f}`", inline=False
        )
        embed.add_field(name="Delta 偏移量", value=f"`{delta_shift:+.2f}`", inline=True)

        exposure_shift_dollars = (
            delta_shift * 670.0
        )  # Assuming SPY=670 for beta-delta dollar mapping
        embed.add_field(
            name="等值曝險變動 (USD)",
            value=f"`${exposure_shift_dollars:,.2f}`",
            inline=True,
        )

        risk_status = "🔴 危險" if abs(adj_delta) > 100 else "🟢 安全"
        embed.add_field(name="風險狀態判定", value=f"**{risk_status}**", inline=False)

        embed.set_footer(text="NRO Vanna Simulation | Nexus Seeker")
        await interaction.followup.send(embed=embed)


async def setup(bot):
    await bot.add_cog(CalendarCog(bot))
