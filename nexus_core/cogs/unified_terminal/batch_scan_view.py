from typing import Any
import discord
from typing import List
import logging
from cogs.embed_builder import create_error_embed, chunk_embeds

logger = logging.getLogger(__name__)


class BatchScanCopyAllButton(discord.ui.Button):
    """
    按鈕：點擊後將所有頁數的數據轉換為文字區塊並發送，以便使用者複製。
    """

    def __init__(self, all_embeds: List[discord.Embed]):
        super().__init__(
            label="📋 複製全部數據",
            style=discord.ButtonStyle.secondary,
            row=0,
        )
        self.all_embeds = all_embeds

    async def callback(self, interaction: discord.Interaction) -> Any:
        await interaction.response.defer(ephemeral=True)
        try:
            for emb in self.all_embeds:
                content = ""
                if emb.title:
                    content += f"**{emb.title}**\n"
                if emb.description:
                    content += emb.description + "\n"
                for field in emb.fields:
                    if field.name and field.value:
                        content += f"**{field.name}**\n{field.value}\n"

                # Split content into chunks of 1900 chars to avoid Discord's 2000 char limit.
                # However, since the description might be wrapped in ```ansi, splitting blindly will break formatting.
                # Usually a single radar embed description is around 2000-3000 chars, so sending as discord.File is actually safer for large text.
                # Wait, if we send it as text, Discord might reject > 2000 chars.
                # Let's chunk the text by taking chunks of 1900 chars.
                chunks = [content[i : i + 1900] for i in range(0, len(content), 1900)]
                for chunk in chunks:
                    # check if the chunk is mostly ansi block
                    if (
                        "```ansi" in chunk
                        and "```" not in chunk[chunk.find("```ansi") + 7 :]
                    ):
                        chunk += "\n```"
                    elif "```" in chunk and chunk.count("```") % 2 != 0:
                        chunk += "\n```"
                    await interaction.followup.send(chunk, ephemeral=True)
        except Exception as e:
            logger.error(f"Copy All Button error: {e}")
            await interaction.followup.send(
                embed=create_error_embed(f"複製數據失敗: {e}"), ephemeral=True
            )


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

                        # 尋找雙星號包裹的粗體標的代號，例如 **AAPL**
                        symbols = re.findall(r"\*\*([A-Za-z0-9.-]+)\*\*", field.value)
                        warning_symbols = [s.upper() for s in symbols]
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


class BatchScanView(discord.ui.View):
    """
    批次掃描總覽面板的互動 View。
    已移除「選擇單一標的深入分析」下拉選單。
    """

    def __init__(
        self,
        symbols: List[str],
        cog: Any,
        bot: Any,
        all_embeds: List[discord.Embed] | None = None,
    ):
        super().__init__(timeout=300)
        self.add_item(BatchScanWarningButton(cog, bot))
        if all_embeds:
            self.add_item(BatchScanCopyAllButton(all_embeds))
