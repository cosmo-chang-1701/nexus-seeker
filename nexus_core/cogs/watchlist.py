import discord
from discord.ext import commands
from discord import app_commands
import asyncio
import logging

import database
import market_math
from cogs.embed_builder import create_scan_embed

import math

from ui.watchlist import WatchlistPagination

logger = logging.getLogger(__name__)


class WatchlistCog(commands.Cog):
    """觀察清單 (Watchlist) 管理指令 — 綁定 user_id"""

    def __init__(self, bot):
        self.bot = bot
        logger.info("WatchlistCog loaded.")

    @app_commands.command(name="add_watch", description="將股票代號加入您的雷達掃描清單")
    @app_commands.describe(
        symbol="股票代號 (如 TSLA)",
        stock_cost="預設 0。輸入您的持有現股平均成本 (將精確計算防禦區間)",
        use_llm="預設 False。是否啟用 LLM 語意風控 (會消耗 Token)"
    )
    async def add_watch(self, interaction: discord.Interaction, symbol: str, stock_cost: float = 0.0, use_llm: bool = False):
        symbol = symbol.upper()
        user_id = interaction.user.id
        success = database.add_watchlist_symbol(user_id, symbol, stock_cost, use_llm)
        if success:
            cc_tag = " 🛡️(Covered)" if stock_cost > 0.0 else ""
            llm_tag = " 🤖(LLM Enabled)" if use_llm else ""
            await interaction.response.send_message(f"👁️ 已將 `{symbol} {cc_tag}{llm_tag}` 加入您的觀察清單！開盤將自動私訊精算結果。", ephemeral=True)
        else:
            await interaction.response.send_message(f"⚠️ `{symbol}` 已經在您的觀察清單中了。", ephemeral=True)

    @app_commands.command(name="list_watch", description="列出您的雷達觀察清單")
    async def list_watch(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)

        user_id = interaction.user.id
        symbols_data = database.get_user_watchlist(user_id)
        if not symbols_data:
            await interaction.followup.send("📭 您的觀察清單是空的。", ephemeral=True)
            return

        view = WatchlistPagination(symbols_data)
        view.update_buttons()
        await interaction.followup.send(embed=view.create_embed(), view=view, ephemeral=True)

    @app_commands.command(name="edit_watch", description="編輯觀察清單中的標的設定")
    @app_commands.describe(
        symbol="股票代號 (如 TSLA)",
        stock_cost="[選填] 修改現股平均成本價 (輸入 0 可取消 Covered Call 模式)",
        use_llm="[選填] 是否啟用 LLM 新聞風控審查 (True/False)"
    )
    async def edit_watch(
        self, 
        interaction: discord.Interaction, 
        symbol: str, 
        stock_cost: float = None,
        use_llm: bool = None
    ):
        symbol = symbol.upper()
        
        # 1. 防呆：檢查是否至少輸入了一項要修改的參數
        if stock_cost is None and use_llm is None:
            await interaction.response.send_message(
                "⚠️ 請至少提供 `stock_cost` 或 `use_llm` 其中一項來進行修改！", 
                ephemeral=True
            )
            return

        # 2. 檢查標的是否存在於資料庫
        # (這裡假設您從 database.__init__ 統一匯出了 watchlist)
        existing = database.get_user_watchlist_by_symbol(interaction.user.id, symbol)
        
        if not existing:
            await interaction.response.send_message(
                f"❌ 您的觀察清單中沒有找到 **{symbol}**！請先使用 `/add_watch` 將其加入。", 
                ephemeral=True
            )
            return
            
        # 3. 執行資料庫更新
        success = database.update_user_watchlist(interaction.user.id, symbol, stock_cost, use_llm)
        
        if success:
            # 4. 組裝精美的更新成功回報訊息
            msg_parts = []
            if stock_cost is not None:
                if stock_cost > 0:
                    msg_parts.append(f"📦 現股成本已更新為 `${stock_cost:.2f}` (啟用 Covered Call)")
                else:
                    msg_parts.append("🛑 現股成本歸零 (轉為 Naked Call 高規格風控)")
                    
            if use_llm is not None:
                status_text = "🟢 啟用" if use_llm else "🔴 關閉"
                msg_parts.append(f"🤖 LLM 審查已 {status_text}")
                
            reply_text = f"✅ 已成功更新 **{symbol}** 的雷達設定：\n" + "\n".join([f"└ {msg}" for msg in msg_parts])
            await interaction.response.send_message(reply_text, ephemeral=True)
            
        else:
            await interaction.response.send_message("⚠️ 系統異常：更新失敗，請稍後再試。", ephemeral=True)

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
        await interaction.response.defer(ephemeral=True)
        result = await asyncio.to_thread(market_math.analyze_symbol, symbol)
        if result:
            from services import llm_service, news_service, reddit_service
            news_text = await news_service.fetch_recent_news(symbol)
            reddit_text = await reddit_service.get_reddit_context(symbol)
            ai_verdict = await llm_service.evaluate_trade_risk(symbol, result['strategy'], news_text, reddit_text)

            result['news_text'] = news_text
            result['reddit_text'] = reddit_text
            result['ai_decision'] = ai_verdict.get('decision', 'APPROVE')
            result['ai_reasoning'] = ai_verdict.get('reasoning', '無資料')

            user_capital = database.get_user_capital(interaction.user.id)
            embed = create_scan_embed(result, user_capital)
            await interaction.followup.send(embed=embed)
        else:
            await interaction.followup.send(f"📊 目前 `{symbol.upper()}` 無明確訊號或無合適合約。")

async def setup(bot):
    await bot.add_cog(WatchlistCog(bot))
