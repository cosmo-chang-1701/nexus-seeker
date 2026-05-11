import discord
from discord.ext import commands
from discord import app_commands
import asyncio
import logging
from datetime import datetime
from typing import Optional

from market_analysis.sentiment_engine import SentimentEngine
from cogs.embed_builder import create_sentiment_scan_embed

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

            skew_data, pcr_data, uoa_data, max_pain_data = await asyncio.gather(
                skew_task, pcr_task, uoa_task, max_pain_task
            )

            if "error" in skew_data and skew_data["error"] == "No expiries":
                return await interaction.followup.send(
                    f"❌ 無法取得 `{symbol}` 的期權數據，請檢查標的是否支援期權交易。"
                )

            embed = create_sentiment_scan_embed(
                symbol, skew_data, pcr_data, uoa_data, max_pain_data
            )
            await interaction.followup.send(embed=embed)

        except Exception as e:
            logger.error(f"[{symbol}] skew_scan 失敗: {e}")
            await interaction.followup.send(f"❌ 執行掃描時發生錯誤: {e}")

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
                return await interaction.followup.send(f"❌ 計算失敗: {data['error']}")

            embed = discord.Embed(
                title=f"📍 {symbol} 最大痛點分析 (Max Pain)",
                color=discord.Color.blue(),
                timestamp=datetime.now(),
            )

            embed.add_field(name="到期日", value=f"`{data['expiry']}`", inline=True)
            embed.add_field(
                name="最大痛點 Strike", value=f"**${data['max_pain']}**", inline=True
            )
            embed.add_field(
                name="目前價格", value=f"`${data['current_price']}`", inline=True
            )

            dist = data["distance_pct"]
            dist_str = (
                f"現價高於痛點 `{dist}%`"
                if dist > 0
                else f"現價低於痛點 `{abs(dist)}%`"
            )
            embed.add_field(name="偏離度", value=dist_str, inline=False)

            if data["is_converging"]:
                embed.description = "🎯 **價格正向最大痛點收斂中** (預期結算日波動縮小)"

            # 檢查 DTE < 3 並給予獲利鎖定建議
            expiry_dt = datetime.strptime(data["expiry"], "%Y-%m-%d")
            dte = (expiry_dt - datetime.now()).days
            if dte <= 3:
                embed.add_field(
                    name="🚀 執行建議",
                    value="⚠️ **DTE < 3 且接近最大痛點**\n建議提升 **獲利鎖定 (Profit Lock)** 優先級，規避結算震盪。",
                    inline=False,
                )

            embed.set_footer(text="Nexus Seeker | Execution Automation")
            await interaction.followup.send(embed=embed)

        except Exception as e:
            logger.error(f"[{symbol}] max_pain 失敗: {e}")
            await interaction.followup.send("❌ 執行計算時發生錯誤。")


async def setup(bot):
    await bot.add_cog(SentimentCog(bot))
