import discord
import logging
import database
from services import market_data_service

from cogs.embed_builder import (
    create_error_embed,
    create_strategic_dash_embed,
    create_trades_embed,
    create_holdings_embed,
    build_vtr_stats_embed,
)

logger = logging.getLogger(__name__)


class PortfolioHubView(discord.ui.View):
    """
    Interactive view for the Portfolio Hub (/dash).
    """

    def __init__(self, user_id: int, bot):
        super().__init__(timeout=300)
        self.user_id = user_id
        self.bot = bot

    async def _set_loading(self, interaction: discord.Interaction):
        for child in self.children:
            if isinstance(child, discord.ui.Button):
                child.disabled = True
        await interaction.edit_original_response(view=self)

    async def _reset_loading(self, interaction: discord.Interaction, embed=None):
        for child in self.children:
            if isinstance(child, discord.ui.Button):
                child.disabled = False
        await interaction.edit_original_response(embed=embed, view=self)

    @discord.ui.button(
        label="🏠 戰略看板",
        style=discord.ButtonStyle.success,
        custom_id="btn_home_port",
    )
    async def btn_home(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ):
        await interaction.response.defer()
        await self._set_loading(interaction)
        embed = None
        try:
            from services.trading_service import TradingService
            from services.asset_manager import AssetManager
            from models.asset import ContextType, HoldingMetadata
            from market_analysis.pro_management import calculate_financial_runway

            trading_service = TradingService(self.bot)
            pnl_data = await trading_service.get_portfolio_pnl(self.user_id)
            ctx = database.get_full_user_context(self.user_id)

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

            macro_raw = await market_data_service.get_macro_environment()
            vix_spot = macro_raw.get("vix", 18.0)

            embed = create_strategic_dash_embed(
                ctx,
                pnl_data,
                vix_spot=vix_spot,
                backup_liquidity=backup_liq,
                extended_runway=ext_runway,
            )
        except Exception as e:
            await interaction.followup.send(
                embed=create_error_embed(f"恢復戰略看板失敗: {e}"),
                ephemeral=True,
            )
        finally:
            await self._reset_loading(interaction, embed=embed)

    @discord.ui.button(label="📋 實單持倉", style=discord.ButtonStyle.primary)
    async def btn_trades(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ):
        await interaction.response.defer()
        await self._set_loading(interaction)
        embed = None
        try:
            from services.trading_service import TradingService

            trading_service = TradingService(self.bot)
            pnl_data = await trading_service.get_portfolio_pnl(self.user_id)
            ctx = database.get_full_user_context(self.user_id)
            embed = create_trades_embed(pnl_data, ctx.capital)
        except Exception as e:
            await interaction.followup.send(
                embed=create_error_embed(f"獲取持倉失敗: {e}"), ephemeral=True
            )
        finally:
            await self._reset_loading(interaction, embed=embed)

    @discord.ui.button(label="📦 現貨持倉", style=discord.ButtonStyle.primary)
    async def btn_holdings(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ):
        await interaction.response.defer()
        await self._set_loading(interaction)
        embed = None
        try:
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
        except Exception as e:
            await interaction.followup.send(
                embed=create_error_embed(f"獲取現貨失敗: {e}"), ephemeral=True
            )
        finally:
            await self._reset_loading(interaction, embed=embed)

    @discord.ui.button(label="👻 VTR 績效", style=discord.ButtonStyle.secondary)
    async def btn_vtr(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ):
        await interaction.response.defer()
        await self._set_loading(interaction)
        embed = None
        try:
            from market_analysis.ghost_trader import GhostTrader
            from market_analysis.attribution import AttributionEngine

            await AttributionEngine.finalize_vtr_attribution(self.user_id)
            stats = await GhostTrader.get_vtr_performance_stats(self.user_id)
            attr_lines = AttributionEngine.format_attribution_report(self.user_id)
            embed = build_vtr_stats_embed(
                interaction.user.display_name, stats, attr_lines
            )
        except Exception as e:
            await interaction.followup.send(
                embed=create_error_embed(f"獲取 VTR 績效失敗: {e}"),
                ephemeral=True,
            )
        finally:
            await self._reset_loading(interaction, embed=embed)

    @discord.ui.button(label="🚨 壓力測試", style=discord.ButtonStyle.danger)
    async def btn_stress_test(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ):
        await interaction.response.defer()
        await self._set_loading(interaction)
        embed = None
        try:
            from database.orders import get_user_active_orders

            orders = get_user_active_orders(self.user_id)
            total_deficit = 0.0
            gtc_buy_orders = []
            for o in orders:
                validity = o.get("validity", "").upper()
                side = o.get("side", "").upper()
                if "GTC" in validity and side == "BUY":
                    price = o.get("limit_price", 0.0)
                    if price <= 0.0:
                        price = o.get("stop_price", 0.0)
                    qty = o.get("quantity", 0.0)
                    total_deficit += price * qty
                    gtc_buy_orders.append(o)
            ctx = database.get_full_user_context(self.user_id)
            cash_reserve = ctx.cash_reserve if ctx else 0.0

            from database.holdings import get_user_holdings

            holdings = get_user_holdings(self.user_id)
            boxx_shares = 0.0
            for h in holdings:
                if h.get("symbol", "").upper() == "BOXX":
                    boxx_shares = h.get("quantity", 0.0)
                    break
            boxx_cash = min(boxx_shares, 180.0) * (21000.0 / 180.0)
            net_deficit = cash_reserve + boxx_cash - total_deficit
            is_critical = total_deficit > (cash_reserve + boxx_cash)

            results = {
                "total_deficit": total_deficit,
                "cash_reserve": cash_reserve,
                "boxx_shares": boxx_shares,
                "boxx_cash": boxx_cash,
                "net_deficit": net_deficit,
                "is_critical": is_critical,
                "gtc_buy_orders_count": len(gtc_buy_orders),
            }
            from cogs.embed_builder import create_stress_test_embed

            embed = create_stress_test_embed(results)
        except Exception as e:
            await interaction.followup.send(
                embed=create_error_embed(f"壓力測試失敗: {e}"), ephemeral=True
            )
        finally:
            await self._reset_loading(interaction, embed=embed)
