from typing import Any
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

    def __init__(self, user_id: int, bot: Any) -> Any:  # type: ignore
        super().__init__(timeout=300)
        self.user_id = user_id
        self.bot = bot

    async def _set_loading(self, interaction: discord.Interaction) -> Any:
        for child in self.children:
            if isinstance(child, discord.ui.Button):
                child.disabled = True
        await interaction.edit_original_response(view=self)

    async def _reset_loading(
        self, interaction: discord.Interaction, embed: Any = None
    ) -> Any:
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
    ) -> Any:
        await interaction.response.defer()
        await self._set_loading(interaction)
        embed = None
        try:
            from services.trading_service import TradingService
            from services.withdrawal_runway_service import get_runway_display

            trading_service = TradingService(self.bot)
            pnl_data = await trading_service.get_portfolio_pnl(self.user_id)
            ctx = database.get_full_user_context(self.user_id)
            runway, runway_stale = await get_runway_display(self.user_id)

            vix_spot = await market_data_service.get_vix_spot_strict()

            nav_data = await trading_service.get_market_nav(
                self.user_id, float(ctx.cash_reserve or 0.0), pnl_data
            )
            spy_quote = await market_data_service.get_quote("SPY")
            spy_raw = spy_quote.get("c") if spy_quote else None
            spy_now = float(spy_raw) if spy_raw and float(spy_raw) > 0 else None

            embed = create_strategic_dash_embed(
                ctx,
                pnl_data,
                vix_spot=vix_spot,
                runway=runway,
                runway_stale=runway_stale,
                nav_data=nav_data,
                spy_price=spy_now,
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
    ) -> Any:
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
    ) -> Any:
        await interaction.response.defer()
        await self._set_loading(interaction)
        embed = None
        try:
            from services.asset_manager import AssetManager
            from models.asset import ContextType

            manager = AssetManager()
            assets = manager.get_assets(self.user_id, ContextType.HOLDING)
            from cogs.terminal.holdings import build_holdings_display_rows

            holdings = await build_holdings_display_rows(assets)
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
    ) -> Any:
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
    ) -> Any:
        await interaction.response.defer()
        await self._set_loading(interaction)
        embed = None
        try:
            from services.gtc_stress_test_service import run_gtc_stress_test

            results = await run_gtc_stress_test(self.user_id)
            from cogs.embed_builder import create_stress_test_embed

            embed = create_stress_test_embed(results)
        except Exception as e:
            await interaction.followup.send(
                embed=create_error_embed(f"壓力測試失敗: {e}"), ephemeral=True
            )
        finally:
            await self._reset_loading(interaction, embed=embed)
