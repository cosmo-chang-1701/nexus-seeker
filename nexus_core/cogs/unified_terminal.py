import discord
from discord.ext import commands
from discord import app_commands
import asyncio
import logging
from datetime import datetime, timezone

from services import market_data_service, news_service, reddit_service
from market_analysis.sentiment_engine import SentimentEngine
from market_analysis.psq_engine import analyze_psq
from market_analysis.risk_engine import MacroContext
import market_math
import database
from cogs.embed_builder import (
    create_scan_embed,
    create_sentiment_scan_embed,
    create_news_scan_embed,
    create_reddit_scan_embed,
    create_trades_embed,
    create_holdings_embed,
    build_vtr_stats_embed,
    create_polymarket_list_embed,
)

logger = logging.getLogger(__name__)


class SymbolHubView(discord.ui.View):
    """
    Interactive view for the Unified Symbol Hub (/x).
    """

    def __init__(self, symbol: str, user_id: int, bot):
        super().__init__(timeout=300)
        self.symbol = symbol.upper()
        self.user_id = user_id
        self.bot = bot
        self.base_data = {}

    @discord.ui.button(
        label="📰 新聞分析", style=discord.ButtonStyle.primary, custom_id="btn_news"
    )
    async def btn_news(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ):
        await interaction.response.defer()
        try:
            news_text = await news_service.fetch_recent_news(self.symbol)
            embed = create_news_scan_embed(self.symbol, news_text)
            await interaction.followup.send(embed=embed, ephemeral=True)
        except Exception as e:
            await interaction.followup.send(f"❌ 獲取新聞失敗: {e}", ephemeral=True)

    @discord.ui.button(
        label="💬 Reddit 情緒",
        style=discord.ButtonStyle.primary,
        custom_id="btn_reddit",
    )
    async def btn_reddit(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ):
        await interaction.response.defer()
        try:
            reddit_text = await reddit_service.get_reddit_context(self.symbol)
            embed = create_reddit_scan_embed(self.symbol, reddit_text)
            await interaction.followup.send(embed=embed, ephemeral=True)
        except Exception as e:
            await interaction.followup.send(
                f"❌ 獲取 Reddit 情緒失敗: {e}", ephemeral=True
            )

    @discord.ui.button(
        label="📐 情緒掃描",
        style=discord.ButtonStyle.secondary,
        custom_id="btn_sentiment",
    )
    async def btn_sentiment(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ):
        await interaction.response.defer()
        try:
            skew_task = SentimentEngine.calculate_skew(self.symbol)
            pcr_task = SentimentEngine.calculate_pcr(self.symbol)
            uoa_task = SentimentEngine.detect_uoa(self.symbol)
            max_pain_task = SentimentEngine.calculate_max_pain(self.symbol)

            skew_data, pcr_data, uoa_data, max_pain_data = await asyncio.gather(
                skew_task, pcr_task, uoa_task, max_pain_task
            )
            embed = create_sentiment_scan_embed(
                self.symbol, skew_data, pcr_data, uoa_data, max_pain_data
            )
            await interaction.followup.send(embed=embed, ephemeral=True)
        except Exception as e:
            await interaction.followup.send(f"❌ 執行情緒掃描失敗: {e}", ephemeral=True)

    @discord.ui.button(
        label="🎯 最大痛點",
        style=discord.ButtonStyle.secondary,
        custom_id="btn_maxpain",
    )
    async def btn_maxpain(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ):
        await interaction.response.defer()
        try:
            data = await SentimentEngine.calculate_max_pain(self.symbol)
            if "error" in data:
                return await interaction.followup.send(
                    f"❌ 計算失敗: {data['error']}", ephemeral=True
                )

            embed = discord.Embed(
                title=f"📍 {self.symbol} 最大痛點分析 (Max Pain)",
                color=discord.Color.blue(),
                timestamp=datetime.now(),
            )
            embed.add_field(name="到期日", value=f"`{data['expiry']}`", inline=True)
            embed.add_field(
                name="最大痛點 Strike", value=f"**${data['max_pain']}**", inline=True
            )
            embed.add_field(
                name="目前價格", value=f"`${data['current_price']}`", inline=True
            )
            dist = data["distance_pct"]
            dist_str = (
                f"現價高於痛點 `{dist}%`"
                if dist > 0
                else f"現價低於痛點 `{abs(dist)}%`"
            )
            embed.add_field(name="偏離度", value=dist_str, inline=False)
            embed.set_footer(text="Nexus Seeker | Execution Automation")
            await interaction.followup.send(embed=embed, ephemeral=True)
        except Exception as e:
            await interaction.followup.send(f"❌ 計算最大痛點失敗: {e}", ephemeral=True)


class PortfolioHubView(discord.ui.View):
    """
    Interactive view for the Portfolio Hub (/dash).
    """

    def __init__(self, user_id: int, bot):
        super().__init__(timeout=300)
        self.user_id = user_id
        self.bot = bot

    @discord.ui.button(label="📋 實單持倉", style=discord.ButtonStyle.primary)
    async def btn_trades(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ):
        await interaction.response.defer()
        from services.trading_service import TradingService

        trading_service = TradingService(self.bot)
        pnl_data = await trading_service.get_portfolio_pnl(self.user_id)
        ctx = database.get_full_user_context(self.user_id)
        embed = create_trades_embed(pnl_data, ctx.capital)
        await interaction.edit_original_response(embed=embed, view=self)

    @discord.ui.button(label="📦 現貨持倉", style=discord.ButtonStyle.primary)
    async def btn_holdings(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ):
        await interaction.response.defer()
        from services.asset_manager import AssetManager
        from models.asset import ContextType

        manager = AssetManager()
        assets = manager.get_assets(self.user_id, ContextType.HOLDING)
        holdings = []
        for a in assets:
            quote = await market_data_service.get_quote(a.symbol)
            h_data = {
                "symbol": a.symbol,
                "quantity": a.metadata.get("quantity", 0.0),
                "avg_cost": a.metadata.get("avg_cost", 0.0),
                "current_price": quote.get("c", 0.0) if quote else 0.0,
            }
            holdings.append(h_data)
        ctx = database.get_full_user_context(self.user_id)
        embed = create_holdings_embed(holdings, ctx.capital)
        await interaction.edit_original_response(embed=embed, view=self)

    @discord.ui.button(label="🏁 財務跑道", style=discord.ButtonStyle.secondary)
    async def btn_runway(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ):
        await interaction.response.defer()
        from market_analysis.portfolio import refresh_portfolio_greeks
        from market_analysis.pro_management import calculate_financial_runway
        from services.asset_manager import AssetManager
        from models.asset import ContextType, HoldingMetadata

        await refresh_portfolio_greeks(self.user_id)
        ctx = database.get_full_user_context(self.user_id)
        runway_days = calculate_financial_runway(
            ctx.cash_reserve, ctx.monthly_expense, ctx.total_theta
        )
        manager = AssetManager()
        holdings = manager.get_assets(self.user_id, ContextType.HOLDING)
        total_holding_value = 0.0
        for h in holdings:
            meta = HoldingMetadata(**h.metadata)
            quote = await market_data_service.get_quote(h.symbol)
            total_holding_value += (
                quote.get("c", 0.0) if quote else 0.0
            ) * meta.quantity
        backup_liq = total_holding_value * 0.8
        ext_runway = calculate_financial_runway(
            ctx.cash_reserve + backup_liq, ctx.monthly_expense, ctx.total_theta
        )
        embed = discord.Embed(
            title="🏁 財務生存跑道分析",
            color=discord.Color.green(),
            timestamp=datetime.now(timezone.utc),
        )
        embed.add_field(
            name="💰 現金儲備", value=f"`${ctx.cash_reserve:,.2f}`", inline=True
        )
        embed.add_field(
            name="📉 每月支出", value=f"`${ctx.monthly_expense:,.2f}`", inline=True
        )
        embed.add_field(
            name="💸 每日 Theta", value=f"`+${ctx.total_theta:,.2f}/day`", inline=True
        )
        embed.add_field(
            name="⌛ 核心生存跑道", value=f"**{runway_days:,.1f} 天**", inline=False
        )
        if backup_liq > 0:
            embed.add_field(
                name="🛡️ 備用流動性",
                value=f"含 HOLDING 淨值後預計可達: **{ext_runway:,.1f} 天**",
                inline=False,
            )
        await interaction.edit_original_response(embed=embed, view=self)

    @discord.ui.button(label="👻 VTR 績效", style=discord.ButtonStyle.secondary)
    async def btn_vtr(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ):
        await interaction.response.defer()
        from market_analysis.ghost_trader import GhostTrader
        from market_analysis.attribution import AttributionEngine

        await AttributionEngine.finalize_vtr_attribution(self.user_id)
        stats = await GhostTrader.get_vtr_performance_stats(self.user_id)
        attr_lines = AttributionEngine.format_attribution_report(self.user_id)
        embed = build_vtr_stats_embed(interaction.user.display_name, stats, attr_lines)
        await interaction.edit_original_response(embed=embed, view=self)


class PulseHubView(discord.ui.View):
    """
    Interactive view for the Pulse Hub (/market).
    """

    def __init__(self, user_id: int, bot):
        super().__init__(timeout=300)
        self.user_id = user_id
        self.bot = bot

    @discord.ui.button(label="📅 市場日曆", style=discord.ButtonStyle.primary)
    async def btn_calendar(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ):
        await interaction.response.defer()
        from services.calendar_service import calendar_service

        events = await calendar_service.get_portfolio_events(self.user_id)
        if not events:
            return await interaction.edit_original_response(
                content="📭 未來 7 日內無影響持倉標的的重大事件或財報。",
                embed=None,
                view=self,
            )

        embed = discord.Embed(
            title="📅 【 重大市場事件 & 財報日曆 】",
            color=discord.Color.blue(),
            timestamp=datetime.now(),
        )
        for event in events[:15]:
            if event["type"] == "ECONOMIC":
                impact = "🔴" if event["impact"].lower() == "high" else "🟡"
                embed.add_field(
                    name=f"{impact} {event['event']} ({event['country']})",
                    value=f"⏰ TTE: `{event['tte_hours']}`h | `{event['time']}`",
                    inline=False,
                )
            else:
                embed.add_field(
                    name=f"📊 {event['symbol']} 財報發布",
                    value=f"⏰ TTE: `{event['tte_hours']}`h | `{event['date']}`",
                    inline=False,
                )
        await interaction.edit_original_response(embed=embed, view=self)

    @discord.ui.button(label="🐋 預測市場", style=discord.ButtonStyle.primary)
    async def btn_poly(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ):
        await interaction.response.defer()
        if not hasattr(self.bot, "polymarket_service"):
            return await interaction.edit_original_response(
                content="❌ Polymarket 服務未初始化。", embed=None, view=self
            )
        markets = self.bot.polymarket_service.get_active_markets(limit=20)
        embed = create_polymarket_list_embed(markets)
        await interaction.edit_original_response(embed=embed, view=self)

    @discord.ui.button(label="🔥 高波動掃描", style=discord.ButtonStyle.secondary)
    async def btn_iv(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer()
        from market_analysis.volatility_inspector import VolatilityInspector

        all_watchlists = database.get_all_watchlist()
        user_watch = [row[1] for row in all_watchlists if row[0] == self.user_id]
        if not user_watch:
            return await interaction.edit_original_response(
                content="📭 觀察清單為空，無法執行 IV 掃描。", embed=None, view=self
            )

        inspector = VolatilityInspector(self.bot)
        results = await inspector.run_scan(user_watch, self.user_id)
        high_iv = [
            r for r in results if r.get("iv_rank", 0) > 80 or r.get("is_high_risk_vol")
        ]

        if not high_iv:
            return await interaction.edit_original_response(
                content="🔎 未發現 IV Rank > 80% 的高波動標的。", embed=None, view=self
            )

        embed = discord.Embed(
            title="🔥 【 高波動 & IV Crush 風險掃描 】", color=discord.Color.red()
        )
        for res in high_iv[:15]:
            risk = "🚨" if res["is_high_risk_vol"] else "⚠️"
            embed.add_field(
                name=f"{risk} {res['symbol']} (IVR: {res['iv_rank']}%)",
                value=f"TTE: `{res['tte_hours']:.1f}`h | 策略: {res['strategy']}",
                inline=False,
            )
        await interaction.edit_original_response(embed=embed, view=self)


class UnifiedTerminalCog(commands.Cog):
    """
    Unified Hubs for Nexus Seeker.
    Consolidates 20+ commands into 3 core hubs: /x, /dash, /market.
    """

    def __init__(self, bot):
        self.bot = bot
        logger.info("UnifiedTerminalCog loaded.")

    @app_commands.command(
        name="x", description="🌌 標體分析中心：一站式獲取報價、量化掃描與情緒分析"
    )
    @app_commands.describe(symbol="股票代號 (如 NVDA)")
    async def symbol_hub(self, interaction: discord.Interaction, symbol: str):
        await interaction.response.defer(ephemeral=True)
        symbol = symbol.upper()
        user_id = interaction.user.id

        if not await market_data_service.validate_symbol(symbol):
            return await interaction.followup.send(
                f"❌ **無效的標的代號**: `{symbol}`", ephemeral=True
            )

        try:
            from services.asset_manager import AssetManager
            from models.asset import ContextType

            manager = AssetManager()
            assets = manager.get_assets(user_id, ContextType.HOLDING)
            stock_cost = next(
                (a.metadata.get("avg_cost", 0.0) for a in assets if a.symbol == symbol),
                0.0,
            )

            spy_task = market_data_service.get_spy_history_df("1y")
            macro_task = market_data_service.get_macro_environment()
            df_spy, macro_raw = await asyncio.gather(spy_task, macro_task)
            spy_price = df_spy["Close"].iloc[-1] if not df_spy.empty else 670.0
            macro_data = MacroContext(
                vix=macro_raw.get("vix", 18.0),
                oil_price=macro_raw.get("oil", 75.0),
                vix_change=macro_raw.get("vix_change", 0.0),
            )

            result = await market_math.analyze_symbol(
                symbol, stock_cost, df_spy, spy_price, vix_spot=macro_data.vix
            )
            if not result:
                result = {"symbol": symbol, "stock_cost": stock_cost, "price": 0.0}

            df_hist_1d = await market_data_service.get_history_df(
                symbol, period="1y", interval="1d"
            )
            psq_result = analyze_psq(df_hist_1d, vix_spot=macro_data.vix)
            if psq_result:
                result["psq_result"] = psq_result
                result["price"] = (
                    df_hist_1d["Close"].iloc[-1]
                    if not df_hist_1d.empty
                    else result.get("price", 0.0)
                )

            skew_data = await SentimentEngine.calculate_skew(symbol)
            result["skew"] = skew_data.get("skew", 0.0)
            result["vix"] = macro_data.vix
            result["spy_price"] = spy_price

            user_context = database.get_full_user_context(user_id)

            if "strategy" in result:
                main_embed = create_scan_embed(result, user_context.capital)
            else:
                # 查無信號時，建立基礎報價 Embed
                quote = await market_data_service.get_quote(symbol)
                main_embed = discord.Embed(
                    title=f"💹 {symbol} 基礎行情 (查無量化訊號)",
                    color=discord.Color.blue()
                    if quote.get("dp", 0) >= 0
                    else discord.Color.red(),
                    timestamp=datetime.now(timezone.utc),
                )
                main_embed.add_field(
                    name="現價 (Current)",
                    value=f"**${quote.get('c', 0.0)}**",
                    inline=True,
                )
                main_embed.add_field(
                    name="漲跌幅 (%)", value=f"`{quote.get('dp', 0.0)}%`", inline=True
                )
                main_embed.add_field(
                    name="VIX / SPY",
                    value=f"`{macro_data.vix:.1f}` / `${spy_price:.1f}`",
                    inline=True,
                )
                main_embed.set_footer(
                    text="Nexus Seeker | 點擊下方按鈕獲取進一步深度分析"
                )

            view = SymbolHubView(symbol, user_id, self.bot)
            view.base_data = result

            await interaction.followup.send(embed=main_embed, view=view, ephemeral=True)

        except Exception as e:
            logger.error(f"Symbol Hub Error for {symbol}: {e}")
            await interaction.followup.send(
                f"❌ 載入 `{symbol}` 資料時發生錯誤: {e}", ephemeral=True
            )

    @app_commands.command(
        name="dash", description="📊 交易員看板：一站式監控持倉、跑道與 VTR 績效"
    )
    async def portfolio_hub(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        user_id = interaction.user.id

        from services.trading_service import TradingService

        trading_service = TradingService(self.bot)
        pnl_data = await trading_service.get_portfolio_pnl(user_id)
        ctx = database.get_full_user_context(user_id)
        embed = create_trades_embed(pnl_data, ctx.capital)

        view = PortfolioHubView(user_id, self.bot)
        await interaction.followup.send(embed=embed, view=view, ephemeral=True)

    @app_commands.command(
        name="market", description="🌌 市場情報中心：監控日曆、預測市場與高波動標的"
    )
    async def pulse_hub(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        from services.calendar_service import calendar_service

        events = await calendar_service.get_portfolio_events(interaction.user.id)
        embed = discord.Embed(
            title="📅 【 重大市場事件 & 財報日曆 】",
            color=discord.Color.blue(),
            timestamp=datetime.now(),
        )
        if not events:
            embed.description = "📭 未來 7 日內無重大事件。"
        else:
            for event in events[:10]:
                if event["type"] == "ECONOMIC":
                    impact = "🔴" if event["impact"].lower() == "high" else "🟡"
                    embed.add_field(
                        name=f"{impact} {event['event']}",
                        value=f"⏰ TTE: `{event['tte_hours']}`h",
                        inline=False,
                    )
                else:
                    embed.add_field(
                        name=f"📊 {event['symbol']} 財報",
                        value=f"⏰ TTE: `{event['tte_hours']}`h",
                        inline=False,
                    )

        view = PulseHubView(interaction.user.id, self.bot)
        await interaction.followup.send(embed=embed, view=view, ephemeral=True)


async def setup(bot):
    await bot.add_cog(UnifiedTerminalCog(bot))
