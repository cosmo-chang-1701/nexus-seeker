from typing import Any, Optional
import discord
import math
from datetime import datetime
from cogs.embed_builder import create_watchlist_embed

_SORT_LABELS = {
    "created": "加入時間",
    "alpha": "字母 A→Z",
    "tags": "標籤",
}


def _apply_sort_and_filter(
    data: list, sort_key: Optional[str], query: Optional[str]
) -> list:
    """依 sort_key 排序、query 過濾觀察清單資料 (symbol, tags, created_at) 三元組。"""
    filtered = data
    if query:
        q = query.strip().upper()
        filtered = [
            row
            for row in data
            if q in row[0].upper() or (row[1] and q in row[1].upper())
        ]

    if sort_key == "alpha":
        return sorted(filtered, key=lambda row: row[0])
    if sort_key == "tags":
        return sorted(filtered, key=lambda row: (row[1] or "", row[0]))
    # 預設：依加入時間排序 (created_at 可能為 None，視為最早)
    return sorted(filtered, key=lambda row: row[2] or datetime.min)


class WatchlistPagination(discord.ui.View):
    def __init__(
        self,
        data: Any,
        original_interaction: discord.Interaction | None = None,
        sort_key: Optional[str] = None,
        query: Optional[str] = None,
    ):
        super().__init__(timeout=180)  # 3 分鐘後按鈕失效
        self.sort_key = sort_key
        self.query = query
        self.data = _apply_sort_and_filter(data, sort_key, query)
        self.original_interaction = original_interaction
        self.current_page = 1
        self.items_per_page = 50  # 每頁顯示數量
        self.total_pages = (
            math.ceil(len(self.data) / self.items_per_page) if self.data else 1
        )

    # 生成當前頁面的 Embed
    def create_embed(self) -> Any:
        # 切片取得當前頁面的資料
        start_idx = (self.current_page - 1) * self.items_per_page
        end_idx = start_idx + self.items_per_page
        page_data = [
            (sym, tags) for sym, tags, _created_at in self.data[start_idx:end_idx]
        ]

        return create_watchlist_embed(
            page_data,
            self.current_page,
            self.total_pages,
            len(self.data),
            sort_label=_SORT_LABELS.get(self.sort_key or "created"),
            query=self.query,
        )

    # 更新按鈕狀態 (如果在第一頁就禁用上一頁，以此類推)
    def update_buttons(self) -> None:
        self.prev_button.disabled = self.current_page == 1
        self.next_button.disabled = self.current_page == self.total_pages

    @discord.ui.button(
        label="◀ 上一頁", style=discord.ButtonStyle.primary, custom_id="prev"
    )
    async def prev_button(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> Any:
        self.current_page -= 1
        self.update_buttons()
        await interaction.response.edit_message(embed=self.create_embed(), view=self)

    @discord.ui.button(
        label="下一頁 ▶", style=discord.ButtonStyle.primary, custom_id="next"
    )
    async def next_button(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> Any:
        self.current_page += 1
        self.update_buttons()
        await interaction.response.edit_message(embed=self.create_embed(), view=self)

    async def _refresh_data(self, user_id: int) -> None:
        """重新從資料庫取得觀察清單，並套用目前的排序/過濾設定。"""
        from services.asset_manager import AssetManager
        from models.asset import ContextType

        manager = AssetManager()
        assets = manager.get_assets(user_id, ContextType.WATCH)
        raw_data = [(a.symbol, getattr(a, "tags", None), a.created_at) for a in assets]
        self.data = _apply_sort_and_filter(raw_data, self.sort_key, self.query)
        self.total_pages = (
            math.ceil(len(self.data) / self.items_per_page) if self.data else 1
        )
        if self.current_page > self.total_pages:
            self.current_page = max(1, self.total_pages)
        self.update_buttons()

    @discord.ui.button(
        label="🏷️ 原地編輯標籤",
        style=discord.ButtonStyle.secondary,
        custom_id="edit_tags",
        row=1,
    )
    async def edit_tags_button(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> Any:
        from ui.watchlist_tags import WatchlistTagSelectView

        original_message = interaction.message

        async def on_success(modal_interaction: discord.Interaction) -> Any:
            # 重新計算清單資料 (套用目前的排序/過濾設定)
            await self._refresh_data(modal_interaction.user.id)

            # 更新原來的 list_watch 訊息
            if self.original_interaction:
                try:
                    await self.original_interaction.edit_original_response(
                        embed=self.create_embed(), view=self
                    )
                except discord.HTTPException:
                    pass
            elif original_message:
                try:
                    await original_message.edit(embed=self.create_embed(), view=self)
                except discord.HTTPException:
                    pass

            # 將互動標籤表單更新為成功提示，製造原地更新的平滑體驗
            from cogs.embed_builders.settings_embeds import create_info_embed

            embed = create_info_embed(
                title="✅ 標籤已更新", message="您的自選清單與標籤已同步刷新。"
            )
            await modal_interaction.response.edit_message(embed=embed, view=None)

        view = WatchlistTagSelectView(
            interaction.user.id, on_success_callback=on_success
        )

        from cogs.embed_builders.settings_embeds import create_info_embed

        embed = create_info_embed(
            title="編輯自選標籤", message="請從下方選單選擇一個自選標的來編輯它的標籤。"
        )
        await interaction.response.send_message(embed=embed, view=view, ephemeral=True)

    @discord.ui.button(
        label="🔔 設定價格警報",
        style=discord.ButtonStyle.secondary,
        custom_id="set_alert",
        row=2,
    )
    async def set_alert_button(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> Any:
        from ui.watchlist_alerts import WatchlistAlertSelectView

        view = WatchlistAlertSelectView(interaction.user.id)

        from cogs.embed_builders.settings_embeds import create_info_embed

        embed = create_info_embed(
            title="設定價格警報",
            message="請從下方選單選擇一個自選標的，設定 15 分鐘價量突破/跌破警報。",
        )
        await interaction.response.send_message(embed=embed, view=view, ephemeral=True)
