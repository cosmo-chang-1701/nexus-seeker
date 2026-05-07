import discord
from discord.ext import commands
from discord import app_commands
import asyncio
import logging

import database
import market_math
from cogs.embed_builder import create_scan_embed
from services import market_data_service
import math

from ui.watchlist import WatchlistPagination

logger = logging.getLogger(__name__)

class WatchlistCog(commands.Cog):
    """觀察清單 (Watchlist) 管理指令 — 綁定 user_id"""

    def __init__(self, bot):
        self.bot = bot
        logger.info("WatchlistCog loaded.")

    @app_commands.command(name="add_watch", description="將股票代號加入您的雷達掃描清單")
    @app_commands.describe(symbol="股票代號 (如 TSLA)", stock_cost="預設 0。輸入您的持有現股平均成本 (將精確計算防禦區間)", use_llm="預設 True。是否啟用 LLM 語意風控")
    async def add_watch(self, interaction: discord.Interaction, symbol: str, stock_cost: float = 0.0, use_llm: bool = True):
        symbol = symbol.upper()
        if database.add_watchlist_symbol(interaction.user.id, symbol, stock_cost, use_llm):
            await interaction.response.send_message(f"👁️ 已將 `{symbol}{' 🛡️(Covered)' if stock_cost > 0.0 else ''}{' 🤖(LLM Enabled)' if use_llm else ''}` 加入您的觀察清單！", ephemeral=True)
        else:
            await interaction.response.send_message(f"⚠️ `{symbol}` 已經在您的觀察清單中了。", ephemeral=True)

    @app_commands.command(name="list_watch", description="列出您的雷達觀察清單")
    async def list_watch(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        symbols_data = database.get_user_watchlist(interaction.user.id)
        if not symbols_data:
            await interaction.followup.send("📭 您的觀察清單是空的。", ephemeral=True)
            return
        view = WatchlistPagination(symbols_data)
        view.update_buttons()
        await interaction.followup.send(embed=view.create_embed(), view=view, ephemeral=True)

    @app_commands.command(name="edit_watch", description="編輯觀察清單中的標的設定")
    async def edit_watch(self, interaction: discord.Interaction, symbol: str, stock_cost: float = None, use_llm: bool = None):
        symbol = symbol.upper()
        if stock_cost is None and use_llm is None:
            await interaction.response.send_message("⚠️ 請至少提供一項參數！", ephemeral=True)
            return
        if not database.get_user_watchlist_by_symbol(interaction.user.id, symbol):
            await interaction.response.send_message(f"❌ 觀察清單中沒有找到 {symbol}。", ephemeral=True)
            return
        if database.update_user_watchlist(interaction.user.id, symbol, stock_cost, use_llm):
            await interaction.response.send_message(f"✅ 已成功更新 **{symbol}** 的雷達設定。", ephemeral=True)
        else:
            await interaction.response.send_message("⚠️ 系統異常：更新失敗。", ephemeral=True)

    @app_commands.command(name="remove_watch", description="將股票代號從您的觀察清單移除")
    async def remove_watch(self, interaction: discord.Interaction, symbol: str):
        symbol = symbol.upper()
        if database.delete_watchlist_symbol(interaction.user.id, symbol):
            await interaction.response.send_message(f"🗑️ 已將 `{symbol}` 從您的觀察清單移除。", ephemeral=True)
        else:
            await interaction.response.send_message(f"❌ 找不到 `{symbol}`。", ephemeral=True)

    @app_commands.command(name="scan", description="執行量化掃描、What-if 模擬與自動風控優化")
    async def manual_scan(self, interaction: discord.Interaction, symbol: str):
        await interaction.response.defer(ephemeral=True)
        user_id, symbol = interaction.user.id, symbol.upper()

        try:
            spy_task = market_data_service.get_spy_history_df("1y")
            macro_task = market_data_service.get_macro_environment()
            df_spy, macro_raw = await asyncio.gather(spy_task, macro_task)
            spy_price = df_spy['Close'].iloc[-1] if not df_spy.empty else 670.0
            from market_analysis.risk_engine import MacroContext
            macro_data = MacroContext(vix=macro_raw.get('vix', 18.0), oil_price=macro_raw.get('oil', 75.0), vix_change=macro_raw.get('vix_change', 0.0))
        except Exception as e:
            logger.warning(f"基準資料抓取異常: {e}")
            df_spy, spy_price, macro_data = None, 670.0, MacroContext(vix=22.0, oil_price=85.0, vix_change=0.0)

        # analyze_symbol 已經是 async
        result = await market_math.analyze_symbol(symbol, 0.0, df_spy, spy_price)
        is_option_valid = bool(result)
        if not result:
            result = {'symbol': symbol, 'stock_cost': 0.0}

        # 🚀 新增 PowerSqueeze 掃描 (使用日 K)
        df_hist_1d = await market_data_service.get_history_df(symbol, period="1y", interval="1d")
        from market_analysis.psq_engine import analyze_psq
        from cogs.embed_builder import create_psq_embed
        psq_result = analyze_psq(df_hist_1d)
        if psq_result:
            result['psq_result'] = psq_result

        embeds_to_send = []

        if is_option_valid:
            from services import llm_service, news_service, reddit_service
            from market_analysis.risk_engine import optimize_position_risk
            
            news_task, reddit_task = news_service.fetch_recent_news(symbol), reddit_service.get_reddit_context(symbol)
            news_text, reddit_text = await asyncio.gather(news_task, reddit_task)
            ai_verdict = await llm_service.evaluate_trade_risk(symbol, result.get('strategy', ''), news_text, reddit_text)

            result.update({'news_text': news_text, 'reddit_text': reddit_text, 'ai_decision': ai_verdict.get('decision', 'APPROVE'), 'ai_reasoning': ai_verdict.get('reasoning', '無資料'), 'vix': macro_data.vix, 'oil': macro_data.oil_price})

            user_context = await asyncio.to_thread(database.get_full_user_context, user_id)
            user_capital, current_total_delta = user_context.capital, user_context.total_weighted_delta
            strategy = result.get('strategy', '')
            
            safe_qty, hedge_spy = optimize_position_risk(current_delta=current_total_delta, unit_weighted_delta=result.get('weighted_delta', 0.0), user_capital=user_capital, spy_price=spy_price, stock_iv=result.get('iv', 0.15), strategy=strategy, macro_data=macro_data, base_risk_limit_pct=user_context.risk_limit_base)

            alloc_pct, margin_per_contract = result.get('alloc_pct', 0.0), result.get('margin_per_contract', 0.0)
            suggested_contracts = int((user_capital * min(alloc_pct, 0.25)) // margin_per_contract) if user_capital > 0 and margin_per_contract > 0 else 0

            projected_total_delta = current_total_delta + (result.get('weighted_delta', 0.0) * (-1 if "STO" in strategy else 1) * safe_qty)
            projected_exposure_pct = (projected_total_delta * spy_price / user_capital) * 100 if user_capital > 0 else 0
            
            result.update({'projected_exposure_pct': round(projected_exposure_pct, 2), 'suggested_contracts': suggested_contracts, 'safe_qty': safe_qty, 'hedge_spy': hedge_spy, 'spy_price': spy_price})
            embeds_to_send.append(create_scan_embed(result, user_capital))

        if psq_result:
            result['price'] = df_hist_1d['Close'].iloc[-1] if not df_hist_1d.empty else 0.0
            embeds_to_send.append(create_psq_embed(result))
            
        if embeds_to_send:
            await interaction.followup.send(embeds=embeds_to_send)
        else:
            await interaction.followup.send(f"📊 目前 `{symbol}` 查無有效訊號，建議維持觀望。")

async def setup(bot):
    await bot.add_cog(WatchlistCog(bot))
