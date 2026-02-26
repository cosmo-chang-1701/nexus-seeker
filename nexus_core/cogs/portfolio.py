import discord
from discord.ext import commands
from discord import app_commands
import logging

from market_analysis.ghost_trader import GhostTrader
from cogs.embed_builder import build_vtr_stats_embed

import database
import database.virtual_trading as vtr_db

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
    @app_commands.describe(
        symbol="股票代號 (如 TSLA)",
        opt_type="買方或賣方策略",
        strike="履約價",
        expiry="到期日 (YYYY-MM-DD)",
        entry_price="成交價格 (權利金)",
        quantity="口數",
        stock_cost="預設 0。輸入您的持有現股平均成本 (將精確計算防禦區間)"
    )
    async def add_trade(self, interaction: discord.Interaction, symbol: str, opt_type: app_commands.Choice[str], strike: float, expiry: str, entry_price: float, quantity: int, stock_cost: float = 0.0):
        symbol = symbol.upper()
        user_id = interaction.user.id
        try:
            trade_id = database.add_portfolio_record(user_id, symbol, opt_type.value, strike, expiry, entry_price, quantity, stock_cost)
            action_text = "賣出 (STO)" if quantity < 0 else "買入 (BTO)"
            # 私訊回覆使用者
            cost_str = f" | 現股成本: ${stock_cost:.2f}" if stock_cost > 0.0 else ""
            await interaction.response.send_message(
                f"✅ **新增成功 (ID: {trade_id})**: {action_text} {abs(quantity)} 口 `{symbol}` ${strike} {opt_type.value.upper()} ({expiry} 到期){cost_str}", 
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
            trade_id, sym, o_type, strike, exp, price, qty, stock_cost = row
            action = "賣出 (STO)" if qty < 0 else "買入 (BTO)"
            cov_str = f" | 現股成本: ${stock_cost:.2f}" if stock_cost > 0.0 else ""
            msg += f"`ID:{trade_id:02d}` | **{sym}** | {exp} 到期 | ${strike} {o_type.upper()} | {action} {abs(qty)}口 | 成本: ${price}{cov_str}\n"
        await interaction.response.send_message(msg, ephemeral=True)

    @app_commands.command(name="remove_trade", description="將部位從您的監控庫中移除")
    async def remove_trade(self, interaction: discord.Interaction, trade_id: int):
        user_id = interaction.user.id
        record = database.delete_portfolio_record(user_id, trade_id)
        if record:
            await interaction.response.send_message(f"🗑️ **已刪除紀錄 (ID: {trade_id})**: `{record[0]}` ${record[1]} {record[2].upper()} 已移除。", ephemeral=True)
        else:
            await interaction.response.send_message(f"❌ 找不到屬於您的 ID `{trade_id}`。", ephemeral=True)

    @app_commands.command(name="vtr_list", description="列出目前虛擬交易室 (VTR) 的所有持倉")
    async def vtr_list(self, interaction: discord.Interaction):
        user_id = interaction.user.id
        rows = vtr_db.get_virtual_trades(user_id=user_id, status='OPEN')
        if not rows:
            await interaction.response.send_message("📭 目前虛擬交易室無任何開啟中的持倉。", ephemeral=True)
            return
            
        msg = "👻 **【虛擬交易室 (VTR) 開啟部位】**\n"
        for row in rows:
            action = "賣出 (STO)" if row['quantity'] < 0 else "買入 (BTO)"
            msg += f"`ID:{row['id']:03d}` | **{row['symbol']}** | {row['expiry']} 到期 | ${row['strike']} {row['opt_type'].upper()} | {action} {abs(row['quantity'])}口 | 建倉價: ${row['entry_price']:.2f}\n"
            
        await interaction.response.send_message(msg, ephemeral=True)

    @app_commands.command(name="vtr_stats", description="顯示虛擬交易室 (VTR) 的績效統計")
    async def vtr_stats(self, interaction: discord.Interaction):
        user_id = interaction.user.id
        rows = vtr_db.get_virtual_trades(user_id=user_id, status='CLOSED')
        rolled_rows = vtr_db.get_virtual_trades(user_id=user_id, status='ROLLED')
        all_closed = rows + rolled_rows
        
        if not all_closed:
            await interaction.response.send_message("📊 目前虛擬交易室尚無已平倉紀錄可供統計。", ephemeral=True)
            return
            
        total_pnl = 0.0
        wins = 0
        losses = 0
        total_win_pnl = 0.0
        total_loss_pnl = 0.0
        
        for trade in all_closed:
            pnl = trade['pnl'] if trade['pnl'] is not None else 0.0
            total_pnl += pnl
            if pnl > 0:
                wins += 1
                total_win_pnl += pnl
            elif pnl < 0:
                losses += 1
                total_loss_pnl += abs(pnl)
                
        total_trades = wins + losses
        win_rate = (wins / total_trades * 100) if total_trades > 0 else 0.0
        
        avg_win = total_win_pnl / wins if wins > 0 else 0.0
        avg_loss = total_loss_pnl / losses if losses > 0 else 0.0
        profit_factor = (total_win_pnl / total_loss_pnl) if total_loss_pnl > 0 else float('inf')
        
        open_count = len(vtr_db.get_virtual_trades(user_id=user_id, status='OPEN'))
        
        embed = discord.Embed(title="📈 虛擬交易室 (VTR) 績效統計", color=discord.Color.blurple())
        embed.add_field(name="總平倉筆數", value=f"{len(all_closed)}", inline=True)
        embed.add_field(name="勝率", value=f"{win_rate:.1f}% ({wins}W / {losses}L)", inline=True)
        embed.add_field(name="總 PnL", value=f"${total_pnl:,.2f}", inline=True)
        
        embed.add_field(name="盈虧比 (PF)", value=f"{profit_factor:.2f}" if profit_factor != float('inf') else "∞", inline=True)
        embed.add_field(name="平均獲利", value=f"${avg_win:,.2f}", inline=True)
        embed.add_field(name="平均虧損", value=f"${avg_loss:,.2f}", inline=True)
        
        embed.add_field(name="目前開啟中部位", value=f"{open_count} 筆", inline=False)
        
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @app_commands.command(name="vtr_stats", description="檢視虛擬交易室的績效統計")
    async def vtr_stats(self, interaction: discord.Interaction):
        # 1. 延遲回覆 (Defer)，因為計算績效需要 Database I/O
        await interaction.response.defer(ephemeral=True)
        
        try:
            # 2. 呼叫 GhostTrader 統計引擎
            stats = GhostTrader.get_vtr_performance_stats(interaction.user.id)
            
            # 3. 渲染 UI
            embed = build_vtr_stats_embed(interaction.user.display_name, stats)
            
            # 4. 回傳臨時訊息
            await interaction.followup.send(embed=embed)
            
        except Exception as e:
            logger.error(f"執行 /vtr_stats 失敗: {e}")
            await interaction.followup.send("❌ 無法獲取績效數據，請確認是否有已結算的虛擬部位。", ephemeral=True)

async def setup(bot):
    await bot.add_cog(PortfolioCog(bot))
