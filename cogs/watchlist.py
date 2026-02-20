import discord
from discord.ext import commands
from discord import app_commands
import asyncio
import logging

import database
import market_math
from cogs.embed_builder import create_scan_embed

logger = logging.getLogger(__name__)


class WatchlistCog(commands.Cog):
    """觀察清單 (Watchlist) 管理指令 — 綁定 user_id"""

    def __init__(self, bot):
        self.bot = bot
        logger.info("WatchlistCog loaded.")

    @app_commands.command(name="add_watch", description="將股票代號加入您的雷達掃描清單")
    @app_commands.describe(
        symbol="股票代號 (如 TSLA)",
        stock_cost="預設 0。輸入您的持有現股平均成本 (將精確計算防禦區間)"
    )
    async def add_watch(self, interaction: discord.Interaction, symbol: str, stock_cost: float = 0.0):
        symbol = symbol.upper()
        user_id = interaction.user.id
        success = database.add_watchlist_symbol(user_id, symbol, stock_cost)
        if success:
            cc_tag = " 🛡️(Covered)" if stock_cost > 0.0 else ""
            await interaction.response.send_message(f"👁️ 已將 `{symbol} {cc_tag}` 加入您的觀察清單！開盤將自動私訊精算結果。", ephemeral=True)
        else:
            await interaction.response.send_message(f"⚠️ `{symbol}` 已經在您的觀察清單中了。", ephemeral=True)

    @app_commands.command(name="list_watch", description="列出您的雷達觀察清單")
    async def list_watch(self, interaction: discord.Interaction):
        user_id = interaction.user.id
        symbols = database.get_user_watchlist(user_id)
        if not symbols:
            await interaction.response.send_message("📭 您的觀察清單是空的。", ephemeral=True)
            return
        msg = "📡 **【您的專屬觀察清單】**\n" + "、".join([f"`{sym}`" for sym in symbols])
        await interaction.response.send_message(msg, ephemeral=True)

    @app_commands.command(name="remove_watch", description="將股票代號從您的觀察清單移除")
    async def remove_watch(self, interaction: discord.Interaction, symbol: str):
        symbol = symbol.upper()
        user_id = interaction.user.id
        if database.delete_watchlist_symbol(user_id, symbol):
            await interaction.response.send_message(f"🗑️ 已將 `{symbol}` 從您的觀察清單移除。", ephemeral=True)
        else:
            await interaction.response.send_message(f"❌ 找不到 `{symbol}`。", ephemeral=True)

    @app_commands.command(name="scan", description="手動對特定股票執行 Delta 中性掃描")
    async def manual_scan(self, interaction: discord.Interaction, symbol: str):
        logger.info(f"User {interaction.user.id} triggered manual_scan for {symbol}")
        await interaction.response.defer(ephemeral=True)
        result = await asyncio.to_thread(market_math.analyze_symbol, symbol.upper())
        if result:
            # 🔥 讀取該名使用者的專屬資金
            user_capital = database.get_user_capital(interaction.user.id)
            embed = create_scan_embed(result, user_capital)
            await interaction.followup.send(embed=embed)
        else:
            await interaction.followup.send(f"📊 目前 `{symbol.upper()}` 無明確訊號或無合適合約。")


async def setup(bot):
    await bot.add_cog(WatchlistCog(bot))
