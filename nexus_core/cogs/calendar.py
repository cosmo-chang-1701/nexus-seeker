from cogs.embed_builder import (
    create_event_impact_embed,
    create_info_embed,
    create_iv_risk_scan_embed,
    build_calendar_embed,
)
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
        name="calendar",
        description="顯示當月份的「重要總經事件」與觀察清單的「個股財報」",
    )
    async def calendar(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        user_id = interaction.user.id

        # 1. 解析當前用戶的自選股清單 (Watchlist)
        from database.watchlist import get_user_watchlist

        watchlist_rows = get_user_watchlist(user_id)
        symbols = [row[0] for row in watchlist_rows]

        # 2. 獲取總經數據 (30 天) 與財報快取數據
        from typing import Any

        macro_events = await calendar_service.get_high_impact_events(days=30)

        earnings_events: list[Any] = []
        if symbols:
            earnings_map = await calendar_service.get_symbol_earnings_batch(symbols)
            for ev in earnings_map.values():
                if ev is not None:
                    earnings_events.append(ev)

            # Sort by tte_hours
            earnings_events.sort(key=lambda x: getattr(x, "tte_hours", float("inf")))

        # 3. 讀取 CME FedWatch 概率
        fedwatch_prob = None
        for macro_ev in macro_events:
            prob = getattr(macro_ev, "fedwatch_probability", None)
            if prob is not None:
                fedwatch_prob = float(prob)
                break

        # 4. 生成 Embed
        embed = build_calendar_embed(
            macro_events=macro_events,
            earnings_events=earnings_events,
            fedwatch_prob=fedwatch_prob,
        )

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
                embed=create_info_embed(
                    title="查無資料", message="📭 觀察清單為空，無法執行 IV 掃描。"
                )
            )

        results = await self.vol_inspector.run_scan(user_watch, user_id)

        # Filter for IV Rank > 80 or high risk vol
        high_iv_results = [
            r for r in results if r.get("iv_rank", 0) > 80 or r.get("is_high_risk_vol")
        ]

        if not high_iv_results:
            return await interaction.followup.send(
                embed=create_info_embed(
                    title="系統資訊",
                    message="📊 掃描完成，未發現 IV Rank > 80% 的高波動標的。",
                )
            )

        embed = create_iv_risk_scan_embed(high_iv_results)
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
                embed=create_info_embed(
                    title="查無資料",
                    message=f"📭 您目前並未持有 `{symbol}` 的部位，無法進行模擬。",
                )
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

        exposure_shift_dollars = (
            delta_shift * 670.0
        )  # Assuming SPY=670 for beta-delta dollar mapping
        embed = create_event_impact_embed(
            symbol=symbol,
            vol_move=vol_move,
            total_delta=total_delta,
            total_vanna=total_vanna,
            adjusted_delta=adj_delta,
            delta_shift=delta_shift,
            exposure_shift_dollars=exposure_shift_dollars,
        )
        await interaction.followup.send(embed=embed)


async def setup(bot):
    await bot.add_cog(CalendarCog(bot))
