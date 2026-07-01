import discord
import logging
import database

from cogs.embed_builder import (
    create_error_embed,
    create_info_embed,
    create_iv_risk_scan_embed,
    create_market_calendar_embed,
    create_polymarket_list_embed,
)
from .utils import get_macro_overview_data

logger = logging.getLogger(__name__)


class PulseHubView(discord.ui.View):
    """
    Interactive view for the Pulse Hub (/market).
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

    @discord.ui.button(label="📊 總經風控", style=discord.ButtonStyle.success)
    async def btn_macro_overview(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ):
        await interaction.response.defer()
        await self._set_loading(interaction)
        embed = None
        try:
            from cogs.embed_builder import build_market_macro_overview_embed

            macro_data = await get_macro_overview_data(self.user_id)
            embed = build_market_macro_overview_embed(macro_data)
        except Exception as e:
            await interaction.followup.send(
                embed=create_error_embed(f"獲取總經數據失敗: {e}"), ephemeral=True
            )
        finally:
            await self._reset_loading(interaction, embed=embed)

    @discord.ui.button(label="📅 市場日曆", style=discord.ButtonStyle.primary)
    async def btn_calendar(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ):
        await interaction.response.defer()
        await self._set_loading(interaction)
        embed = None
        try:
            from services.calendar_service import calendar_service

            events = await calendar_service.get_portfolio_events(self.user_id)
            embed = create_market_calendar_embed(
                events,
                max_items=15,
                empty_message="📭 未來 7 日內無影響持倉標的的重大事件或財報。",
            )
        except Exception as e:
            await interaction.followup.send(
                embed=create_error_embed(f"獲取日曆失敗: {e}"), ephemeral=True
            )
        finally:
            await self._reset_loading(interaction, embed=embed)

    @discord.ui.button(label="🐋 預測市場", style=discord.ButtonStyle.primary)
    async def btn_poly(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ):
        await interaction.response.defer()
        await self._set_loading(interaction)
        embed = None
        try:
            if not hasattr(self.bot, "polymarket_service"):
                embed = create_error_embed(
                    "Polymarket 服務未初始化。", title="系統錯誤"
                )
            else:
                markets = self.bot.polymarket_service.get_active_markets(limit=20)
                embed = create_polymarket_list_embed(markets)
        except Exception as e:
            await interaction.followup.send(
                embed=create_error_embed(f"獲取預測市場失敗: {e}"),
                ephemeral=True,
            )
        finally:
            await self._reset_loading(interaction, embed=embed)

    @discord.ui.button(label="🔥 高波動掃描", style=discord.ButtonStyle.secondary)
    async def btn_iv(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer()
        await self._set_loading(interaction)
        embed = None
        try:
            from market_analysis.volatility_inspector import VolatilityInspector

            all_watchlists = database.get_all_watchlist()
            user_watch = [row[1] for row in all_watchlists if row[0] == self.user_id]
            if not user_watch:
                embed = create_info_embed(
                    "查無資料", "📭 觀察清單為空，無法執行 IV 掃描。"
                )
            else:
                inspector = VolatilityInspector(self.bot)
                results = await inspector.run_scan(user_watch, self.user_id)
                high_iv = [
                    r
                    for r in results
                    if r.get("iv_rank", 0) > 80 or r.get("is_high_risk_vol")
                ]
                embed = create_iv_risk_scan_embed(high_iv)
        except Exception as e:
            await interaction.followup.send(
                embed=create_error_embed(f"執行 IV 掃描失敗: {e}"),
                ephemeral=True,
            )
        finally:
            await self._reset_loading(interaction, embed=embed)
