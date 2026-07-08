import discord
import math
from cogs.embed_builder import create_watchlist_embed


class WatchlistPagination(discord.ui.View):
    def __init__(self, data, original_interaction: discord.Interaction = None):
        super().__init__(timeout=180)  # 3 分鐘後按鈕失效
        self.data = data
        self.original_interaction = original_interaction
        self.current_page = 1
        self.items_per_page = 50  # 每頁顯示數量
        self.total_pages = math.ceil(len(data) / self.items_per_page) if data else 1

    # 生成當前頁面的 Embed
    def create_embed(self):
        # 切片取得當前頁面的資料
        start_idx = (self.current_page - 1) * self.items_per_page
        end_idx = start_idx + self.items_per_page
        page_data = self.data[start_idx:end_idx]

        return create_watchlist_embed(
            page_data, self.current_page, self.total_pages, len(self.data)
        )

    # 更新按鈕狀態 (如果在第一頁就禁用上一頁，以此類推)
    def update_buttons(self):
        self.prev_button.disabled = self.current_page == 1
        self.next_button.disabled = self.current_page == self.total_pages

    @discord.ui.button(
        label="◀ 上一頁", style=discord.ButtonStyle.primary, custom_id="prev"
    )
    async def prev_button(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ):
        self.current_page -= 1
        self.update_buttons()
        await interaction.response.edit_message(embed=self.create_embed(), view=self)

    @discord.ui.button(
        label="下一頁 ▶", style=discord.ButtonStyle.primary, custom_id="next"
    )
    async def next_button(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ):
        self.current_page += 1
        self.update_buttons()
        await interaction.response.edit_message(embed=self.create_embed(), view=self)

    @discord.ui.button(
        label="🏷️ 原地編輯標籤",
        style=discord.ButtonStyle.secondary,
        custom_id="edit_tags",
        row=1,
    )
    async def edit_tags_button(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ):
        from ui.watchlist_tags import WatchlistTagSelectView
        from services.asset_manager import AssetManager
        from models.asset import ContextType
        import math

        original_message = interaction.message

        async def on_success(modal_interaction: discord.Interaction):
            # 重新計算清單資料
            manager = AssetManager()
            assets = manager.get_assets(modal_interaction.user.id, ContextType.WATCH)
            self.data = [
                (a.symbol, a.metadata.get("use_llm", True), getattr(a, "tags", None))
                for a in assets
            ]
            self.total_pages = (
                math.ceil(len(self.data) / self.items_per_page) if self.data else 1
            )
            if self.current_page > self.total_pages:
                self.current_page = max(1, self.total_pages)
            self.update_buttons()

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
