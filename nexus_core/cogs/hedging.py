import discord
from discord.ext import commands
from discord import app_commands
import logging
import sqlite3
import config
from typing import Optional

logger = logging.getLogger(__name__)


class HedgingCog(commands.Cog):
    """
    [Hedging] Automated Hedging & Risk Settlement Terminal.
    Handles hedge confirmation and risk attribution.
    """

    def __init__(self, bot):
        self.bot = bot
        logger.info("HedgingCog loaded.")

    @app_commands.command(name="settle_hedge", description="確認並記錄已執行的對沖操作")
    @app_commands.describe(
        alert_id="對沖警報 ID (見戰位報告底部)", actual_qty="實際執行的合約/股數 (選填)"
    )
    async def settle_hedge(
        self,
        interaction: discord.Interaction,
        alert_id: int,
        actual_qty: Optional[int] = None,
    ):
        await interaction.response.defer(ephemeral=True)
        user_id = interaction.user.id

        try:
            conn = sqlite3.connect(config.DB_NAME)
            cursor = conn.cursor()

            # 1. 驗證警報是否存在且屬於該使用者
            cursor.execute(
                "SELECT * FROM hedge_alerts WHERE id = ? AND user_id = ?",
                (alert_id, user_id),
            )
            alert = cursor.fetchone()

            if not alert:
                return await interaction.followup.send(
                    "❌ 找不到該警報 ID，或您無權操作。", ephemeral=True
                )

            if alert[10] != "PENDING":  # status is at index 10
                return await interaction.followup.send(
                    f"⚠️ 該警報已處理過 (狀態: {alert[10]})。", ephemeral=True
                )

            # 2. 更新狀態為 EXECUTED
            final_qty = (
                actual_qty if actual_qty is not None else alert[8]
            )  # hedge_contracts is at index 8

            cursor.execute(
                """
                UPDATE hedge_alerts
                SET status = 'EXECUTED', executed_at = CURRENT_TIMESTAMP, hedge_contracts = ?
                WHERE id = ?
            """,
                (final_qty, alert_id),
            )

            conn.commit()
            conn.close()

            # 3. 回饋使用者
            embed = discord.Embed(
                title="✅ 對沖結算完成",
                description=f"已成功記錄警報 `#{alert_id}` 的對沖執行紀錄。",
                color=discord.Color.green(),
                timestamp=discord.utils.utcnow(),
            )
            embed.add_field(name="執行標的", value=f"`{alert[6]}`", inline=True)
            embed.add_field(name="執行數量", value=f"`{final_qty}`", inline=True)
            embed.set_footer(text="數據已同步至 SQLite 持久化層，可用於歸因分析。")

            await interaction.followup.send(embed=embed, ephemeral=True)

        except Exception as e:
            logger.error(f"Settle hedge failed: {e}")
            await interaction.followup.send(f"❌ 結算失敗: {e}", ephemeral=True)

    @app_commands.command(name="hedge_list", description="查看最近的對沖警報與執行狀態")
    async def hedge_list(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        user_id = interaction.user.id

        try:
            conn = sqlite3.connect(config.DB_NAME)
            cursor = conn.cursor()
            cursor.execute(
                """
                SELECT id, vix_level, hedge_contracts, status, created_at
                FROM hedge_alerts
                WHERE user_id = ?
                ORDER BY created_at DESC LIMIT 10
            """,
                (user_id,),
            )
            rows = cursor.fetchall()
            conn.close()

            if not rows:
                return await interaction.followup.send(
                    "📭 目前無對沖警報紀錄。", ephemeral=True
                )

            embed = discord.Embed(
                title="📜 最近對沖警報列表", color=discord.Color.blue()
            )

            content = []
            for r in rows:
                status_emoji = (
                    "⏳" if r[3] == "PENDING" else "✅" if r[3] == "EXECUTED" else "❌"
                )
                content.append(
                    f"`#{r[0]}` | {status_emoji} | VIX: `{r[1]:.2f}` | 建議: `{r[2]}`股 | {r[4][:16]}"
                )

            embed.description = "\n".join(content)
            await interaction.followup.send(embed=embed, ephemeral=True)

        except Exception as e:
            logger.error(f"Hedge list failed: {e}")
            await interaction.followup.send(f"❌ 獲取列表失敗: {e}", ephemeral=True)


async def setup(bot):
    await bot.add_cog(HedgingCog(bot))
