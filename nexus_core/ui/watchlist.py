import discord
import math
from cogs.embed_builder import create_watchlist_embed

class WatchlistPagination(discord.ui.View):
    def __init__(self, data):
        super().__init__(timeout=180) # 3 分鐘後按鈕失效
        self.data = data
        self.current_page = 1
        self.items_per_page = 50 # 每頁顯示數量
        self.total_pages = math.ceil(len(data) / self.items_per_page) if data else 1

    # 生成當前頁面的 Embed
    def create_embed(self):
        # 切片取得當前頁面的資料
        start_idx = (self.current_page - 1) * self.items_per_page
        end_idx = start_idx + self.items_per_page
        page_data = self.data[start_idx:end_idx]

        return create_watchlist_embed(page_data, self.current_page, self.total_pages, len(self.data))

    # 更新按鈕狀態 (如果在第一頁就禁用上一頁，以此類推)
    def update_buttons(self):
        self.prev_button.disabled = self.current_page == 1
        self.next_button.disabled = self.current_page == self.total_pages

    @discord.ui.button(label="◀ 上一頁", style=discord.ButtonStyle.primary, custom_id="prev")
    async def prev_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.current_page -= 1
        self.update_buttons()
        await interaction.response.edit_message(embed=self.create_embed(), view=self)

    @discord.ui.button(label="下一頁 ▶", style=discord.ButtonStyle.primary, custom_id="next")
    async def next_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.current_page += 1
        self.update_buttons()
        await interaction.response.edit_message(embed=self.create_embed(), view=self)
