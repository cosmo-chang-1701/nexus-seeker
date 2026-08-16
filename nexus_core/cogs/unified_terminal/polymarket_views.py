"""Polymarket 巨鯨意圖圖譜 — Paginated Interactive View.

提供 PolymarketPaginatedView，在單一 Discord 訊息內透過
◀ / ▶ 按鈕就地 edit_message() 切換 Embed 分頁，
取代舊有的 for-loop followup.send() 多訊息洗版模式。
"""

from typing import Any, List

import discord
import logging

logger = logging.getLogger(__name__)


class PolymarketPaginatedView(discord.ui.View):
    """在單一 Discord 訊息內實現 Polymarket Embed 前後翻頁。

    取代舊有的 for-loop followup.send() 多訊息分頁模式，
    改以 ◀/▶ 按鈕就地 edit_message() 更換 Embed，
    實現零洗版的分頁瀏覽體驗。

    Parameters
    ----------
    embeds : List[discord.Embed]
        由 ``create_polymarket_list_embed()`` 產出的分頁 Embed 列表。
    timeout : float
        閒置超時秒數，預設 300 秒（5 分鐘）。
    """

    def __init__(self, embeds: List[discord.Embed], *, timeout: float = 300.0):
        super().__init__(timeout=timeout)
        self.embeds = embeds
        self.current_page: int = 0
        self._update_button_states()

    def _update_button_states(self) -> None:
        """根據當前頁碼動態切換按鈕 disabled 狀態與頁碼顯示。"""
        self.btn_prev.disabled = self.current_page <= 0
        self.btn_next.disabled = self.current_page >= len(self.embeds) - 1
        self.btn_page_indicator.label = f"{self.current_page + 1} / {len(self.embeds)}"

    @discord.ui.button(label="◀", style=discord.ButtonStyle.secondary)
    async def btn_prev(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> Any:
        """翻至上一頁。"""
        self.current_page = max(0, self.current_page - 1)
        self._update_button_states()
        await interaction.response.edit_message(
            embed=self.embeds[self.current_page], view=self
        )

    @discord.ui.button(
        label="1 / 1", style=discord.ButtonStyle.secondary, disabled=True
    )
    async def btn_page_indicator(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> Any:
        """純頁碼顯示按鈕，不觸發任何動作。"""
        pass

    @discord.ui.button(label="▶", style=discord.ButtonStyle.secondary)
    async def btn_next(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> Any:
        """翻至下一頁。"""
        self.current_page = min(len(self.embeds) - 1, self.current_page + 1)
        self._update_button_states()
        await interaction.response.edit_message(
            embed=self.embeds[self.current_page], view=self
        )

    async def on_timeout(self) -> None:
        """Timeout 後移除所有按鈕，避免殭屍互動元件殘留。"""
        self.clear_items()
