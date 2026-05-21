from cogs.embed_builder import (
    create_error_embed,
    create_hedge_list_embed,
    create_hedge_settlement_embed,
    create_info_embed,
)
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
                    embed=create_error_embed(
                        "找不到該警報 ID，或您無權操作。", title="系統錯誤"
                    ),
                    ephemeral=True,
                )

            if alert[10] != "PENDING":  # status is at index 10
                return await interaction.followup.send(
                    embed=create_error_embed(
                        f"該警報已處理過 (狀態: {alert[10]})。", title="系統警告"
                    ),
                    ephemeral=True,
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
            embed = create_hedge_settlement_embed(
                alert_id=alert_id,
                hedge_instrument=str(alert[6]),
                executed_quantity=int(final_qty),
            )
            await interaction.followup.send(embed=embed, ephemeral=True)

        except Exception as e:
            logger.error(f"Settle hedge failed: {e}")
            await interaction.followup.send(
                embed=create_error_embed(f"結算失敗: {e}", title="系統錯誤"),
                ephemeral=True,
            )

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
                    embed=create_info_embed(
                        title="查無資料", message="📭 目前無對沖警報紀錄。"
                    ),
                    ephemeral=True,
                )

            embed = create_hedge_list_embed(rows)
            await interaction.followup.send(embed=embed, ephemeral=True)

        except Exception as e:
            logger.error(f"Hedge list failed: {e}")
            await interaction.followup.send(
                embed=create_error_embed(f"獲取列表失敗: {e}", title="系統錯誤"),
                ephemeral=True,
            )


async def setup(bot):
    await bot.add_cog(HedgingCog(bot))
