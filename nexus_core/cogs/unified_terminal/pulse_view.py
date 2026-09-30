from typing import Any, Optional
import asyncio
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

    def __init__(self, user_id: int, bot: Any) -> Any:  # type: ignore
        super().__init__(timeout=300)
        self.user_id = user_id
        self.bot = bot
        # 最近一次可編輯本訊息的互動（slash 指令本身或最後一次按鈕點擊），
        # 供 on_timeout 停用按鈕；互動 token 有效 15 分鐘，大於 view 逾時 5 分鐘。
        self.last_interaction: Optional[discord.Interaction] = None

    async def on_timeout(self) -> None:
        """逾時後停用所有按鈕，避免殘留點了只會顯示「此互動失敗」的殭屍按鈕。"""
        for child in self.children:
            if isinstance(child, discord.ui.Button):
                child.disabled = True
        if self.last_interaction is None:
            return
        try:
            await self.last_interaction.edit_original_response(view=self)
        except Exception as e:
            logger.debug(f"PulseHubView 逾時停用按鈕失敗: {e}")

    async def _set_loading(self, interaction: discord.Interaction) -> Any:
        self.last_interaction = interaction
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
        # 守衛：embed=None 時不傳 embed 參數，避免意外清空原始 Embed
        kwargs: dict[str, Any] = {"view": self}
        if embed is not None:
            kwargs["embed"] = embed
        await interaction.edit_original_response(**kwargs)

    @discord.ui.button(label="📊 總經風控", style=discord.ButtonStyle.success)
    async def btn_macro_overview(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> Any:
        await interaction.response.defer()
        await self._set_loading(interaction)
        embed = None
        try:
            from cogs.embed_builder import build_market_macro_overview_embed

            macro_data = await get_macro_overview_data(self.user_id)
            embed = build_market_macro_overview_embed(macro_data)
        except Exception:
            logger.exception("/market 總經風控面板載入失敗")
            await interaction.followup.send(
                embed=create_error_embed("獲取總經數據失敗，請稍後再試。"),
                ephemeral=True,
            )
        finally:
            await self._reset_loading(interaction, embed=embed)

    @discord.ui.button(label="📅 市場日曆", style=discord.ButtonStyle.primary)
    async def btn_calendar(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> Any:
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
        except Exception:
            logger.exception("/market 市場日曆載入失敗")
            await interaction.followup.send(
                embed=create_error_embed("獲取日曆失敗，請稍後再試。"),
                ephemeral=True,
            )
        finally:
            await self._reset_loading(interaction, embed=embed)

    @discord.ui.button(label="🐋 預測市場", style=discord.ButtonStyle.primary)
    async def btn_poly(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> Any:
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
                embeds = create_polymarket_list_embed(markets)
                if embeds:
                    if len(embeds) == 1:
                        embed = embeds[0]  # 單頁：就地替換原始 Embed
                    else:
                        # 多頁：用 followup 送出帶翻頁 View 的獨立訊息，
                        # 原始訊息保留 PulseHubView 不動
                        from cogs.unified_terminal.polymarket_views import (
                            PolymarketPaginatedView,
                        )

                        view = PolymarketPaginatedView(embeds, total_items=len(markets))
                        view.message = await interaction.followup.send(
                            embed=embeds[0], view=view, ephemeral=True, wait=True
                        )
                        embed = None  # 不修改原始 Embed
                else:
                    embed = None
        except Exception:
            logger.exception("/market 預測市場載入失敗")
            await interaction.followup.send(
                embed=create_error_embed("獲取預測市場失敗，請稍後再試。"),
                ephemeral=True,
            )
        finally:
            await self._reset_loading(interaction, embed=embed)

    @discord.ui.button(label="🔥 高波動掃描", style=discord.ButtonStyle.secondary)
    async def btn_iv(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> Any:
        await interaction.response.defer()
        await self._set_loading(interaction)
        embed = None
        try:
            from market_analysis.volatility_inspector import VolatilityInspector

            # 只讀本人清單，且同步 SQLite 讀取移出 event loop（原本在 event loop 上
            # 同步撈出全站所有使用者的觀察清單再於記憶體過濾）
            user_rows = await asyncio.to_thread(
                database.get_user_watchlist, self.user_id
            )
            user_watch = [row[0] for row in user_rows]
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
        except Exception:
            logger.exception("/market 高波動掃描失敗")
            await interaction.followup.send(
                embed=create_error_embed("執行 IV 掃描失敗，請稍後再試。"),
                ephemeral=True,
            )
        finally:
            await self._reset_loading(interaction, embed=embed)
