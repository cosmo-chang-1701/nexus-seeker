import discord
from discord.ext import commands
from discord import app_commands
import logging
from datetime import datetime

from services import reddit_service, news_service, market_data_service
from . import embed_builder

logger = logging.getLogger(__name__)

class Research(commands.Cog):
    """研究 (Research) 管理指令"""
    
    def __init__(self, bot):
        self.bot = bot

    @app_commands.command(name="scan_news", description="掃描特定標的之最新官方新聞")
    @app_commands.describe(
        symbol="股票代號 (例如: TSLA)",
        limit="搜尋結果數量 (預設: 5)"
    )
    async def scan_news(self, interaction: discord.Interaction, symbol: str, limit: int = 5):
        await interaction.response.defer(ephemeral=True)
        
        symbol = symbol.upper()
        try:
            news_text = await news_service.fetch_recent_news(symbol, limit)
            
            embed = embed_builder.create_news_scan_embed(symbol, news_text)
            await interaction.followup.send(embed=embed, ephemeral=True)
            
        except Exception as e:
            logger.error(f"[{symbol}] 新聞掃描失敗: {e}")
            await interaction.followup.send(f"❌ 獲取 {symbol} 新聞時發生錯誤。", ephemeral=True)

    @app_commands.command(name="scan_reddit", description="掃描特定標的之 Reddit 散戶情緒 (過去 24 小時)")
    @app_commands.describe(
        symbol="股票代號 (例如: PLTR)",
        limit="搜尋結果數量 (預設: 5)"
    )
    async def scan_reddit(self, interaction: discord.Interaction, symbol: str, limit: int = 5):
        await interaction.response.defer(ephemeral=True)
        
        symbol = symbol.upper()
        try:
            reddit_text = await reddit_service.get_reddit_context(symbol, limit)
            
            embed = embed_builder.create_reddit_scan_embed(symbol, reddit_text)
            await interaction.followup.send(embed=embed, ephemeral=True)
            
        except Exception as e:
            logger.error(f"[{symbol}] Reddit 掃描失敗: {e}")
            await interaction.followup.send(f"❌ 獲取 {symbol} Reddit 情緒時發生錯誤。", ephemeral=True)

    @app_commands.command(name="quote", description="獲取標的即時報價 (Finnhub)")
    @app_commands.describe(symbol="股票代碼 (例如: AAPL, TSLA, SPY)")
    async def quote(self, interaction: discord.Interaction, symbol: str):
        symbol = symbol.upper()
        await interaction.response.defer(ephemeral=True)
        
        data = market_data_service.get_quote(symbol)
        if not data:
            return await interaction.followup.send(f"❌ 無法取得 `{symbol}` 的報價，請檢查代碼是否正確。", ephemeral=True)

        embed = discord.Embed(
            title=f"💹 {symbol} Real-time Quote",
            color=discord.Color.blue() if data['dp'] >= 0 else discord.Color.red(),
            timestamp=datetime.now()
        )
        
        # 這裡顯示 Finnhub 標準欄位
        embed.add_field(name="現價 (Current)", value=f"**${data['c']}**", inline=True)
        embed.add_field(name="漲跌幅 (%)", value=f"{data['dp']}%")
        embed.add_field(name="今日高/低", value=f"H: {data['h']} / L: {data['l']}", inline=False)
        embed.add_field(name="前收盤 (PC)", value=f"${data['pc']}", inline=True)
        
        await interaction.followup.send(embed=embed, ephemeral=True)

async def setup(bot):
    await bot.add_cog(Research(bot))