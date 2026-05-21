import discord
from discord.ext import commands
from discord import app_commands
import asyncio
import logging
from typing import Optional

from market_analysis.sentiment_engine import SentimentEngine
from cogs.embed_builder import (
    create_error_embed,
    create_max_pain_embed,
    create_sentiment_scan_embed,
)

logger = logging.getLogger(__name__)


class SentimentCog(commands.Cog):
    """
    [Sentiment] Options Sentiment & Volatility Strategist.
    Handles Skew, PCR, Max Pain, and UOA detection.
    """

    def __init__(self, bot):
        self.bot = bot
        logger.info("SentimentCog loaded.")

    @app_commands.command(
        name="skew_scan", description="執行期權偏斜 (Skew) 與市場情緒掃描"
    )
    @app_commands.describe(symbol="股票代碼 (例如: TSLA)")
    async def skew_scan(self, interaction: discord.Interaction, symbol: str):
        symbol = symbol.upper()
        await interaction.response.defer(ephemeral=False)

        try:
            # 並行計算各項指標
            skew_task = SentimentEngine.calculate_skew(symbol)
            pcr_task = SentimentEngine.calculate_pcr(symbol)
            uoa_task = SentimentEngine.detect_uoa(symbol)
            max_pain_task = SentimentEngine.calculate_max_pain(symbol)
            iv_task = SentimentEngine.fetch_and_calculate_iv_metrics(symbol)

            (
                skew_data,
                pcr_data,
                uoa_data,
                max_pain_data,
                iv_data,
            ) = await asyncio.gather(
                skew_task, pcr_task, uoa_task, max_pain_task, iv_task
            )

            if "error" in skew_data and skew_data["error"] == "No expiries":
                return await interaction.followup.send(
                    embed=create_error_embed(
                        f"無法取得 `{symbol}` 的期權數據，請檢查標的是否支援期權交易。",
                        title="系統錯誤",
                    )
                )

            embed = create_sentiment_scan_embed(
                symbol, skew_data, pcr_data, uoa_data, max_pain_data, iv_data
            )
            await interaction.followup.send(embed=embed)

        except Exception as e:
            logger.error(f"[{symbol}] skew_scan 失敗: {e}")
            await interaction.followup.send(
                embed=create_error_embed(f"執行掃描時發生錯誤: {e}", title="系統錯誤")
            )

    @app_commands.command(
        name="max_pain", description="計算特定標的之最大痛點 (Max Pain)"
    )
    @app_commands.describe(
        symbol="股票代碼", expiry="到期日 (YYYY-MM-DD，選填，預設最近到期)"
    )
    async def max_pain(
        self,
        interaction: discord.Interaction,
        symbol: str,
        expiry: Optional[str] = None,
    ):
        symbol = symbol.upper()
        await interaction.response.defer(ephemeral=False)

        try:
            data = await SentimentEngine.calculate_max_pain(symbol, expiry)

            if "error" in data:
                return await interaction.followup.send(
                    embed=create_error_embed(
                        f"計算失敗: {data['error']}", title="系統錯誤"
                    )
                )

            embed = create_max_pain_embed(symbol, data)
            await interaction.followup.send(embed=embed)

        except Exception as e:
            logger.error(f"[{symbol}] max_pain 失敗: {e}")
            await interaction.followup.send(
                embed=create_error_embed("執行計算時發生錯誤。", title="系統錯誤")
            )


async def setup(bot):
    await bot.add_cog(SentimentCog(bot))
