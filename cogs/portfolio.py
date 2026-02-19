import discord
from discord.ext import commands
from discord import app_commands
import logging

import database

logger = logging.getLogger(__name__)


class PortfolioCog(commands.Cog):
    """持倉 (Portfolio) 管理指令 — 綁定 user_id"""

    def __init__(self, bot):
        self.bot = bot
        logger.info("PortfolioCog loaded.")

    @app_commands.command(name="add_trade", description="將新的選擇權部位加入您的專屬監控庫")
    @app_commands.choices(opt_type=[
        app_commands.Choice(name="Put (賣權)", value="put"),
        app_commands.Choice(name="Call (買權)", value="call")
    ])
    async def add_trade(self, interaction: discord.Interaction, symbol: str, opt_type: app_commands.Choice[str], strike: float, expiry: str, entry_price: float, quantity: int):
        symbol = symbol.upper()
        user_id = interaction.user.id
        try:
            trade_id = database.add_portfolio_record(user_id, symbol, opt_type.value, strike, expiry, entry_price, quantity)
            action_text = "賣出 (STO)" if quantity < 0 else "買入 (BTO)"
            # 私訊回覆使用者
            await interaction.response.send_message(
                f"✅ **新增成功 (ID: {trade_id})**: {action_text} {abs(quantity)} 口 `{symbol}` ${strike} {opt_type.value.upper()} ({expiry} 到期)", 
                ephemeral=True
            )
        except Exception as e:
            await interaction.response.send_message(f"❌ 寫入失敗: {e}", ephemeral=True)

    @app_commands.command(name="set_capital", description="設定您的總資金規模，用於精算專屬的凱利建議倉位")
    async def set_capital(self, interaction: discord.Interaction, capital: float):
        if capital <= 0:
            await interaction.response.send_message("❌ 資金必須大於 0。", ephemeral=True)
            return
        user_id = interaction.user.id
        database.set_user_capital(user_id, capital)
        await interaction.response.send_message(f"💰 已將您的專屬總資金設定為 `${capital:,.2f}`", ephemeral=True)

    @app_commands.command(name="list_trades", description="列出您目前資料庫中的所有持倉")
    async def list_trades(self, interaction: discord.Interaction):
        user_id = interaction.user.id
        rows = database.get_user_portfolio(user_id)
        if not rows:
            await interaction.response.send_message("📭 您目前無持倉紀錄。", ephemeral=True)
            return
        msg = "📊 **【您的專屬持倉清單】**\n"
        for row in rows:
            trade_id, sym, o_type, strike, exp, price, qty = row
            action = "賣出 (STO)" if qty < 0 else "買入 (BTO)"
            msg += f"`ID:{trade_id:02d}` | **{sym}** | {exp} 到期 | ${strike} {o_type.upper()} | {action} {abs(qty)}口 | 成本: ${price}\n"
        await interaction.response.send_message(msg, ephemeral=True)

    @app_commands.command(name="remove_trade", description="將部位從您的監控庫中移除")
    async def remove_trade(self, interaction: discord.Interaction, trade_id: int):
        user_id = interaction.user.id
        record = database.delete_portfolio_record(user_id, trade_id)
        if record:
            await interaction.response.send_message(f"🗑️ **已刪除紀錄 (ID: {trade_id})**: `{record[0]}` ${record[1]} {record[2].upper()} 已移除。", ephemeral=True)
        else:
            await interaction.response.send_message(f"❌ 找不到屬於您的 ID `{trade_id}`。", ephemeral=True)


async def setup(bot):
    await bot.add_cog(PortfolioCog(bot))
