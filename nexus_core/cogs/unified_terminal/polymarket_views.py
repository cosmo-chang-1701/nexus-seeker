"""Polymarket 巨鯨意圖圖譜 — Paginated Interactive View.

提供 PolymarketPaginatedView，在單一 Discord 訊息內透過
◀ / ▶ 按鈕就地 edit_message() 切換 Embed 分頁，
取代舊有的 for-loop followup.send() 多訊息洗版模式。
"""

from typing import Any, List, Optional

import discord
import logging

logger = logging.getLogger(__name__)


class PolymarketPaginatedView(discord.ui.View):
    """在單一 Discord 訊息內實現 Polymarket Embed 前後翻頁。

    取代舊有的 for-loop followup.send() 多訊息分頁模式，
    改以 ◀/▶ 按鈕就地 edit_message() 更換 Embed，
    實現零洗版的分頁瀏覽體驗。按鈕樣式與頁碼 Footer 格式沿用 `/list_watch`
    的 `WatchlistPagination` (`ui/watchlist.py`)，維持跨模組換頁介面的
    視覺一致性。

    Parameters
    ----------
    embeds : List[discord.Embed]
        由 ``create_polymarket_list_embed()`` 產出的分頁 Embed 列表。
    timeout : float
        閒置超時秒數，預設 300 秒（5 分鐘）。
    total_items : Optional[int]
        Footer 顯示的總項目數（例如活躍市場總數）；未提供時退化為頁數。
    """

    def __init__(
        self,
        embeds: List[discord.Embed],
        *,
        timeout: float = 300.0,
        total_items: Optional[int] = None,
    ):
        super().__init__(timeout=timeout)
        self.embeds = embeds
        self.current_page: int = 0
        self.total_items = total_items if total_items is not None else len(embeds)
        self._apply_footers()
        self._update_button_states()

    def _apply_footers(self) -> None:
        """比照 /list_watch：頁碼與總項目數寫入 Footer，而非另立指示按鈕。"""
        total_pages = len(self.embeds)
        for idx, emb in enumerate(self.embeds):
            emb.set_footer(
                text=f"頁次: {idx + 1}/{total_pages} ｜ 📊 總項目: {self.total_items}"
            )

    def _update_button_states(self) -> None:
        """根據當前頁碼動態切換換頁按鈕 disabled 狀態。"""
        self.btn_prev.disabled = self.current_page <= 0
        self.btn_next.disabled = self.current_page >= len(self.embeds) - 1

    @discord.ui.button(label="◀ 上一頁", style=discord.ButtonStyle.primary)
    async def btn_prev(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> Any:
        """翻至上一頁。"""
        self.current_page = max(0, self.current_page - 1)
        self._update_button_states()
        await interaction.response.edit_message(
            embed=self.embeds[self.current_page], view=self
        )

    @discord.ui.button(label="下一頁 ▶", style=discord.ButtonStyle.primary)
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
