from typing import Any
import discord
from typing import List, Optional
import logging
from cogs.embed_builder import create_error_embed, chunk_embeds

logger = logging.getLogger(__name__)


class BatchScanWarningButton(discord.ui.Button):
    """
    按鈕：點擊後解析即時聯動警示列出的所有標的並批次執行深入分析。
    """

    def __init__(self, cog: Any, bot: Any):
        super().__init__(
            label="⚡ 批次分析警示標的",
            style=discord.ButtonStyle.primary,
            row=0,
        )
        self.cog = cog
        self.bot = bot

    async def callback(self, interaction: discord.Interaction) -> Any:
        if not interaction.message or not interaction.message.embeds:
            await interaction.response.send_message(
                embed=create_error_embed(
                    "無法讀取當前訊息或 Embed 資料。", title="讀取錯誤"
                ),
                ephemeral=True,
            )
            return

        view = self.view
        if not view:
            return

        # 1. 禁用按鈕與下拉選單以防止重複點擊
        for child in view.children:
            child.disabled = True
        await interaction.response.edit_message(view=view)

        try:
            embed = interaction.message.embeds[0]
            warning_symbols = []

            for field in embed.fields:
                if field.name and "即時聯動警示" in field.name:
                    if field.value:
                        import re

                        # 尋找類似 "• 🚀 TSLA:" 的標的代號
                        symbols = re.findall(r"•.*?([A-Za-z0-9.-]+):", field.value)
                        warning_symbols.extend([s.upper() for s in symbols])
                    break

            if not warning_symbols:
                await interaction.followup.send(
                    embed=create_error_embed(
                        "當前訊息的「即時聯動警示」中沒有列出任何標的，或所有標的皆無異常偏離。",
                        title="無警示標的",
                    ),
                    ephemeral=True,
                )
                return

            # 去重並保持順序
            unique_warnings = []
            for s in warning_symbols:
                if s not in unique_warnings:
                    unique_warnings.append(s)

            user_id = interaction.user.id
            await interaction.followup.send(
                f"🔄 正在批次分析以下 {len(unique_warnings)} 個警示標的: {', '.join(unique_warnings)}...",
                ephemeral=True,
            )

            accumulated_embeds: List[discord.Embed] = []
            for symbol in unique_warnings:
                try:
                    await self.cog._run_single_symbol_hub(
                        interaction,
                        symbol,
                        user_id,
                        embeds_accumulator=accumulated_embeds,
                    )
                except Exception as e:
                    logger.error(f"Batch analysis failed for {symbol}: {e}")

            # Chunk embeds safely by cumulative character length (under 5,500 characters) and size limits (max 10 embeds)
            chunks = chunk_embeds(accumulated_embeds, max_size=5500, max_count=10)
            for chunk in chunks:
                try:
                    await interaction.followup.send(embeds=chunk, ephemeral=True)
                except Exception as send_err:
                    logger.error(
                        f"Failed to send chunk of batch analysis embeds: {send_err}"
                    )
        except Exception as outer_err:
            logger.error(f"Outer Batch Scan Warning Button callback error: {outer_err}")
        finally:
            # 2. 恢復按鈕與下拉選單狀態
            for child in view.children:
                child.disabled = False
            try:
                await interaction.edit_original_response(view=view)
            except Exception as final_err:
                logger.error(
                    f"Failed to edit original response in finally block: {final_err}"
                )


class BatchScanPaginatedView(discord.ui.View):
    """
    批次掃描結果的單一訊息換頁 View。

    按鈕樣式與頁碼 Footer 格式沿用 `/list_watch` 的 `WatchlistPagination`
    (`ui/watchlist.py`)，維持跨模組換頁介面的視覺一致性；換頁機制則是
    `interaction.response.edit_message()` 就地換頁，並整合「批次分析警示
    標的」按鈕。多頁掃描結果只需送出一則 followup 訊息即可完整呈現，避免
    逐頁分別呼叫 `interaction.followup.send()` 撞上 Discord 互動的隱性
    followup 訊息數量上限（錯誤碼 40094）。
    """

    def __init__(
        self,
        embeds: List[discord.Embed],
        cog: Any,
        bot: Any,
        *,
        timeout: float = 300.0,
        total_items: Optional[int] = None,
    ):
        super().__init__(timeout=timeout)
        self.embeds = embeds
        self.current_page = 0
        self.total_items = total_items if total_items is not None else len(embeds)

        # 警示標的分析按鈕獨立一列，避免與換頁按鈕擠在同一排
        warning_button = BatchScanWarningButton(cog, bot)
        warning_button.row = 1
        self.add_item(warning_button)

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

    @discord.ui.button(label="◀ 上一頁", style=discord.ButtonStyle.primary, row=0)
    async def btn_prev(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> Any:
        """翻至上一頁。"""
        self.current_page = max(0, self.current_page - 1)
        self._update_button_states()
        await interaction.response.edit_message(
            embed=self.embeds[self.current_page], view=self
        )

    @discord.ui.button(label="下一頁 ▶", style=discord.ButtonStyle.primary, row=0)
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
