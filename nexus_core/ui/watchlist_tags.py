import discord
import re
from typing import List, Callable, Awaitable, Optional
from services.asset_manager import AssetManager
from models.asset import ContextType
from database.watchlist_tags import set_watchlist_tags, get_watchlist_tags


def sanitize_tags(raw_tags_str: str) -> List[str]:
    """
    Sanitize tags string to a list of clean tags.
    Strips spaces, converts to uppercase, filters invalid/special characters.
    """
    if not raw_tags_str:
        return []
    tags = raw_tags_str.split(",")
    clean_tags = []
    for t in tags:
        t = t.strip().upper()
        # Only allow alphanumeric and underscore
        t = re.sub(r"[^A-Z0-9_]", "", t)
        if t and t not in clean_tags:
            clean_tags.append(t)
    return clean_tags


class WatchlistTagModal(discord.ui.Modal):
    def __init__(
        self,
        user_id: int,
        symbol: str,
        current_tags: List[str],
        on_success_callback: Optional[
            Callable[[discord.Interaction], Awaitable[None]]
        ] = None,
    ):
        super().__init__(title=f"編輯 {symbol} 的標籤")
        self.user_id = user_id
        self.symbol = symbol
        self.on_success_callback = on_success_callback

        self.tags_input: discord.ui.TextInput = discord.ui.TextInput(
            label="標籤 (請用半形逗號分隔)",
            style=discord.TextStyle.short,
            placeholder="例如：CORE, HIGH_IV, LONG_TERM",
            default=",".join(current_tags),
            required=False,
            max_length=100,
        )
        self.add_item(self.tags_input)

    async def on_submit(self, interaction: discord.Interaction):
        raw_tags = self.tags_input.value
        clean_tags = sanitize_tags(raw_tags)

        set_watchlist_tags(str(self.user_id), self.symbol, clean_tags)

        if self.on_success_callback:
            await self.on_success_callback(interaction)
        else:
            from cogs.embed_builders.settings_embeds import create_info_embed

            embed = create_info_embed(
                title="標籤已更新",
                message=f"**{self.symbol}** 的標籤已更新為：`[{', '.join(clean_tags)}]`"
                if clean_tags
                else f"**{self.symbol}** 的標籤已清空。",
            )
            await interaction.response.send_message(embed=embed, ephemeral=True)


class WatchlistTagSelect(discord.ui.Select):
    def __init__(
        self,
        user_id: int,
        options: List[discord.SelectOption],
        on_success_callback: Optional[
            Callable[[discord.Interaction], Awaitable[None]]
        ] = None,
    ):
        super().__init__(
            placeholder="請選擇要編輯標籤的自選標的...",
            options=options,
            custom_id="select_watchlist_tag",
        )
        self.user_id = user_id
        self.on_success_callback = on_success_callback

    async def callback(self, interaction: discord.Interaction):
        if interaction.data is None or not isinstance(interaction.data, dict):
            return
        select_values = interaction.data.get("values")
        if not select_values or not isinstance(select_values, list):
            return

        symbol = str(select_values[0])
        current_tags = get_watchlist_tags(str(self.user_id), symbol)

        modal = WatchlistTagModal(
            self.user_id, symbol, current_tags, self.on_success_callback
        )
        await interaction.response.send_modal(modal)


class WatchlistTagSelectView(discord.ui.View):
    def __init__(
        self,
        user_id: int,
        on_success_callback: Optional[
            Callable[[discord.Interaction], Awaitable[None]]
        ] = None,
    ):
        super().__init__(timeout=180)
        self.user_id = user_id
        self.on_success_callback = on_success_callback

        manager = AssetManager()
        assets = manager.get_assets(user_id, ContextType.WATCH)

        if not assets:
            self.add_item(
                discord.ui.Button(label="您的自選名單目前為空", disabled=True)
            )
            return

        # Max 25 options for discord select
        options = []
        for a in assets[:25]:
            tags = getattr(a, "tags", "")
            desc = f"標籤: {tags}" if tags else "尚未設定標籤"
            options.append(discord.SelectOption(label=a.symbol, description=desc[:100]))

        self.add_item(
            WatchlistTagSelect(self.user_id, options, self.on_success_callback)
        )
