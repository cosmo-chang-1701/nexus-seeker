import discord
from discord import app_commands
from discord.ext import commands
import logging

from cogs.embed_builder import create_cc_recovery_embed, create_error_embed
from market_analysis.trading_orchestration import filter_cc_recovery_targets

logger = logging.getLogger(__name__)


class CoveredCallRecoveryCog(commands.Cog):
    """
    [CC Recovery] Filter and display optimal OTM Covered Call contracts.
    """

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        logger.info("CoveredCallRecoveryCog loaded.")

    @app_commands.command(
        name="cc_recovery",
        description="Filter and display optimal OTM Covered Call contracts (DTE 30-50, Delta < 0.15, Yield >= 10%)",
    )
    @app_commands.describe(symbol="The target equity ticker symbol (e.g., NVDA, AMD)")
    async def cc_recovery(self, interaction: discord.Interaction, symbol: str):
        """
        Executes isolated quantitative filtering and renders the results via NexusEmbed.
        """
        # Mypy type-safety checks
        if interaction.message is not None:
            pass

        # 1. Defer interaction to allow database/network processing
        await interaction.response.defer(ephemeral=False)

        try:
            # 2. Call local cache, apply Fallbacks if needed, and run filtration
            data = await filter_cc_recovery_targets(symbol)

            if (
                data is None
                or not data.get("current_price")
                or data["current_price"] <= 0
            ):
                embed = create_error_embed(
                    f"無法取得 `{symbol}` 的最新市價與期權鏈數據，請檢查標的代碼是否正確或是否支援期權。",
                    title="查詢錯誤",
                )
                await interaction.followup.send(embed=embed)
                return

            # 3. Construct and send output using NexusEmbed
            embed = create_cc_recovery_embed(data)
            await interaction.followup.send(embed=embed)

        except Exception as e:
            logger.error(f"[{symbol}] cc_recovery command failed: {e}", exc_info=True)
            embed = create_error_embed(
                f"執行 Covered Call 篩選時發生系統錯誤: {e}",
                title="系統錯誤",
            )
            await interaction.followup.send(embed=embed)


async def setup(bot: commands.Bot):
    await bot.add_cog(CoveredCallRecoveryCog(bot))
