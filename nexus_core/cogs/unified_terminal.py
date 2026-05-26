import discord
from discord.ext import commands
from discord import app_commands
import asyncio
import logging
from typing import Dict, Any

from services import market_data_service, news_service, reddit_service
from market_analysis.sentiment_engine import SentimentEngine
from market_analysis.psq_engine import analyze_psq
from market_analysis.risk_engine import MacroContext
import market_math
import database
from cogs.embed_builder import (
    create_error_embed,
    create_financial_runway_embed,
    create_info_embed,
    create_iv_risk_scan_embed,
    create_market_calendar_embed,
    create_sentiment_scan_embed,
    create_media_sentiment_embed,
    create_trades_embed,
    create_strategic_dash_embed,
    create_tactical_symbol_embed,
    create_tactical_hedge_embed,
    create_holdings_embed,
    build_vtr_stats_embed,
    create_polymarket_list_embed,
)

logger = logging.getLogger(__name__)


class SymbolHubView(discord.ui.View):
    """
    Interactive view for the Unified Symbol Hub (/x).
    Updates the original message in-place and provides loading feedback.
    """

    def __init__(self, symbol: str, user_id: int, bot):
        super().__init__(timeout=300)
        self.symbol = symbol.upper()
        self.user_id = user_id
        self.bot = bot
        self.base_data: Dict[str, Any] = {}

    async def _set_loading(self, interaction: discord.Interaction):
        """將所有按鈕設為禁用狀態以表示讀取中"""
        for child in self.children:
            if isinstance(child, discord.ui.Button):
                child.disabled = True
        await interaction.edit_original_response(view=self)

    async def _reset_loading(self, interaction: discord.Interaction, embed=None):
        """恢復按鈕狀態並更新內容"""
        for child in self.children:
            if isinstance(child, discord.ui.Button):
                child.disabled = False
        await interaction.edit_original_response(embed=embed, view=self)

    @discord.ui.button(
        label="🏠 核心指標", style=discord.ButtonStyle.success, custom_id="btn_home"
    )
    async def btn_home(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ):
        await interaction.response.defer()
        await self._set_loading(interaction)
        embed = None
        try:
            embed = create_tactical_symbol_embed(self.base_data)
        except Exception as e:
            await interaction.followup.send(
                embed=create_error_embed(f"恢復主頁失敗: {e}"), ephemeral=True
            )
        finally:
            await self._reset_loading(interaction, embed=embed)

    @discord.ui.button(
        label="📐 期權情緒",
        style=discord.ButtonStyle.primary,
        custom_id="btn_sentiment",
    )
    async def btn_sentiment(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ):
        await interaction.response.defer()
        await self._set_loading(interaction)
        embed = None
        try:
            skew_task = SentimentEngine.calculate_skew(self.symbol)
            pcr_task = SentimentEngine.calculate_pcr(self.symbol)
            uoa_task = SentimentEngine.detect_uoa(self.symbol)
            max_pain_task = SentimentEngine.calculate_max_pain(self.symbol)
            iv_task = SentimentEngine.fetch_and_calculate_iv_metrics(self.symbol)

            (
                skew_data,
                pcr_data,
                uoa_data,
                max_pain_data,
                iv_data,
            ) = await asyncio.gather(
                skew_task, pcr_task, uoa_task, max_pain_task, iv_task
            )
            embed = create_sentiment_scan_embed(
                self.symbol, skew_data, pcr_data, uoa_data, max_pain_data, iv_data
            )
        except Exception as e:
            await interaction.followup.send(
                embed=create_error_embed(f"執行期權情緒分析失敗: {e}"),
                ephemeral=True,
            )
        finally:
            await self._reset_loading(interaction, embed=embed)

    @discord.ui.button(
        label="🎭 輿情社群",
        style=discord.ButtonStyle.primary,
        custom_id="btn_media",
    )
    async def btn_media(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ):
        await interaction.response.defer()
        await self._set_loading(interaction)
        embed = None
        try:
            news_task = news_service.fetch_recent_news(self.symbol)
            reddit_task = reddit_service.get_reddit_context(self.symbol)
            news_text, reddit_text = await asyncio.gather(news_task, reddit_task)
            embed = create_media_sentiment_embed(self.symbol, news_text, reddit_text)
        except Exception as e:
            await interaction.followup.send(
                embed=create_error_embed(f"獲取輿情社群失敗: {e}"),
                ephemeral=True,
            )
        finally:
            await self._reset_loading(interaction, embed=embed)

    @discord.ui.button(
        label="🔄 即時整理",
        style=discord.ButtonStyle.secondary,
        custom_id="btn_refresh",
    )
    async def btn_refresh(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ):
        await interaction.response.defer()
        await self._set_loading(interaction)
        embed = None
        try:
            # 清除該 symbol 在 sentiment_engine 中的 BoundedCache 快取
            from market_analysis.sentiment_engine import _iv_cache

            if self.symbol in _iv_cache:
                del _iv_cache[self.symbol]
                logger.info(f"[{self.symbol}] 按鈕觸發：已清除 IV 數據快取")

            # 重新加載最新的基礎數據分析結果
            spy_task = market_data_service.get_spy_history_df("1y")
            macro_task = market_data_service.get_macro_environment()
            df_spy, macro_raw = await asyncio.gather(spy_task, macro_task)
            spy_price = df_spy["Close"].iloc[-1] if not df_spy.empty else 670.0
            macro_data = MacroContext(
                vix=macro_raw.get("vix", 18.0),
                oil_price=macro_raw.get("oil", 75.0),
                vix_change=macro_raw.get("vix_change", 0.0),
            )

            # 獲取 stock_cost
            from services.asset_manager import AssetManager
            from models.asset import ContextType

            manager = AssetManager()
            assets = manager.get_assets(self.user_id, ContextType.HOLDING)
            stock_cost = next(
                (
                    a.metadata.get("avg_cost", 0.0)
                    for a in assets
                    if a.symbol == self.symbol
                ),
                0.0,
            )

            result = await market_math.analyze_symbol(
                self.symbol, stock_cost, df_spy, spy_price, vix_spot=macro_data.vix
            )
            if not result:
                result = {"symbol": self.symbol, "stock_cost": stock_cost, "price": 0.0}

            df_hist_1d = await market_data_service.get_history_df(
                self.symbol, period="1y", interval="1d"
            )
            psq_result = analyze_psq(df_hist_1d, vix_spot=macro_data.vix)
            if psq_result:
                result["psq_result"] = psq_result
                result["price"] = (
                    df_hist_1d["Close"].iloc[-1]
                    if not df_hist_1d.empty
                    else result.get("price", 0.0)
                )

            skew_data = await SentimentEngine.calculate_skew(self.symbol)
            result["skew"] = skew_data.get("skew", 0.0)
            result["skew_percentile"] = SentimentEngine.get_indicator_percentile(
                self.symbol, "SKEW", result["skew"]
            )

            result["vix"] = macro_data.vix
            result["spy_price"] = spy_price

            from market_analysis.ddp_inspector import DDPInspector

            ddp_inspector = DDPInspector(self.bot)
            ddp_report = await ddp_inspector.inspect_symbol(self.symbol)
            result["is_ddp"] = ddp_report.get("is_ddp", False) if ddp_report else False

            iv_metrics = await SentimentEngine.fetch_and_calculate_iv_metrics(
                self.symbol
            )
            result["iv_rank"] = iv_metrics.iv_rank

            max_pain_data = await SentimentEngine.calculate_max_pain(self.symbol)
            result["max_pain"] = max_pain_data.get("max_pain", 0.0)

            reddit_text = await reddit_service.get_reddit_context(self.symbol)
            if "看多" in reddit_text or "Bullish" in reddit_text:
                result["reddit_sentiment_score"] = "🚀 樂觀 (Bullish)"
            elif "看空" in reddit_text or "Bearish" in reddit_text:
                result["reddit_sentiment_score"] = "💀 恐慌 (Bearish)"
            else:
                result["reddit_sentiment_score"] = "⚖️ 中性"

            from services.polymarket_service import PolymarketService

            poly_service = PolymarketService(self.bot)
            poly_markets = await poly_service.get_market_snapshot(limit=10)
            poly_odds = "N/A"
            for m in poly_markets:
                if self.symbol.lower() in m.get("question", "").lower():
                    tokens = m.get("tokens", [])
                    if tokens:
                        poly_odds = f"{tokens[0].get('outcome', 'Yes')}: {float(tokens[0].get('price', 0))*100:.1f}%"
                    break
            result["polymarket_odds"] = poly_odds

            self.base_data = result
            embed = create_tactical_symbol_embed(self.base_data)
            await interaction.followup.send(
                embed=create_info_embed(
                    title="更新成功",
                    message=f"✨ `{self.symbol}` 最新數據已重整並更新！",
                ),
                ephemeral=True,
            )
        except Exception as e:
            await interaction.followup.send(
                embed=create_error_embed(f"重整數據失敗: {e}"), ephemeral=True
            )
        finally:
            await self._reset_loading(interaction, embed=embed)

    @discord.ui.button(
        label="🛡️ 一鍵對沖",
        style=discord.ButtonStyle.danger,
        custom_id="btn_hedge",
    )
    async def btn_hedge(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ):
        await interaction.response.defer()
        await self._set_loading(interaction)
        try:
            # 根據目前波動率與情緒自動引導對沖操作
            ivr = self.base_data.get("iv_rank", 50.0)
            rec_strategy = (
                "Bull Put Spread (賣出認沽價差策略)"
                if ivr > 50.0
                else "Bear Debits / Put Protection (買入保護性認沽)"
            )

            embed_hedge = create_tactical_hedge_embed(self.symbol, ivr, rec_strategy)
            await interaction.followup.send(embed=embed_hedge, ephemeral=True)
        except Exception as e:
            await interaction.followup.send(
                embed=create_error_embed(f"開啟對沖中心失敗: {e}"), ephemeral=True
            )
        finally:
            await self._reset_loading(interaction)


class PortfolioHubView(discord.ui.View):
    """
    Interactive view for the Portfolio Hub (/dash).
    """

    def __init__(self, user_id: int, bot):
        super().__init__(timeout=300)
        self.user_id = user_id
        self.bot = bot

    async def _set_loading(self, interaction: discord.Interaction):
        for child in self.children:
            if isinstance(child, discord.ui.Button):
                child.disabled = True
        await interaction.edit_original_response(view=self)

    async def _reset_loading(self, interaction: discord.Interaction, embed=None):
        for child in self.children:
            if isinstance(child, discord.ui.Button):
                child.disabled = False
        await interaction.edit_original_response(embed=embed, view=self)

    @discord.ui.button(
        label="🏠 戰略看板",
        style=discord.ButtonStyle.success,
        custom_id="btn_home_port",
    )
    async def btn_home(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ):
        await interaction.response.defer()
        await self._set_loading(interaction)
        embed = None
        try:
            from services.trading_service import TradingService

            trading_service = TradingService(self.bot)
            pnl_data = await trading_service.get_portfolio_pnl(self.user_id)
            ctx = database.get_full_user_context(self.user_id)

            macro_raw = await market_data_service.get_macro_environment()
            vix_spot = macro_raw.get("vix", 18.0)

            embed = create_strategic_dash_embed(ctx, pnl_data, vix_spot=vix_spot)
        except Exception as e:
            await interaction.followup.send(
                embed=create_error_embed(f"恢復戰略看板失敗: {e}"),
                ephemeral=True,
            )
        finally:
            await self._reset_loading(interaction, embed=embed)

    @discord.ui.button(label="📋 實單持倉", style=discord.ButtonStyle.primary)
    async def btn_trades(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ):
        await interaction.response.defer()
        await self._set_loading(interaction)
        embed = None
        try:
            from services.trading_service import TradingService

            trading_service = TradingService(self.bot)
            pnl_data = await trading_service.get_portfolio_pnl(self.user_id)
            ctx = database.get_full_user_context(self.user_id)
            embed = create_trades_embed(pnl_data, ctx.capital)
        except Exception as e:
            await interaction.followup.send(
                embed=create_error_embed(f"獲取持倉失敗: {e}"), ephemeral=True
            )
        finally:
            await self._reset_loading(interaction, embed=embed)

    @discord.ui.button(label="📦 現貨持倉", style=discord.ButtonStyle.primary)
    async def btn_holdings(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ):
        await interaction.response.defer()
        await self._set_loading(interaction)
        embed = None
        try:
            from services.asset_manager import AssetManager
            from models.asset import ContextType

            manager = AssetManager()
            assets = manager.get_assets(self.user_id, ContextType.HOLDING)
            holdings = []
            for a in assets:
                quote = await market_data_service.get_quote(a.symbol)
                h_data = {
                    "symbol": a.symbol,
                    "quantity": a.metadata.get("quantity", 0.0),
                    "avg_cost": a.metadata.get("avg_cost", 0.0),
                    "current_price": quote.get("c", 0.0) if quote else 0.0,
                }
                holdings.append(h_data)
            ctx = database.get_full_user_context(self.user_id)
            embed = create_holdings_embed(holdings, ctx.capital)
        except Exception as e:
            await interaction.followup.send(
                embed=create_error_embed(f"獲取現貨失敗: {e}"), ephemeral=True
            )
        finally:
            await self._reset_loading(interaction, embed=embed)

    @discord.ui.button(label="🏁 財務跑道", style=discord.ButtonStyle.secondary)
    async def btn_runway(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ):
        await interaction.response.defer()
        await self._set_loading(interaction)
        embed = None
        try:
            from market_analysis.portfolio import refresh_portfolio_greeks
            from market_analysis.pro_management import calculate_financial_runway
            from services.asset_manager import AssetManager
            from models.asset import ContextType, HoldingMetadata

            await refresh_portfolio_greeks(self.user_id)
            ctx = database.get_full_user_context(self.user_id)
            runway_days = calculate_financial_runway(
                ctx.cash_reserve, ctx.monthly_expense, ctx.total_theta
            )
            manager = AssetManager()
            holdings = manager.get_assets(self.user_id, ContextType.HOLDING)
            total_holding_value = 0.0
            for h in holdings:
                meta = HoldingMetadata(**h.metadata)
                quote = await market_data_service.get_quote(h.symbol)
                total_holding_value += (
                    quote.get("c", 0.0) if quote else 0.0
                ) * meta.quantity
            backup_liq = total_holding_value * 0.8
            ext_runway = calculate_financial_runway(
                ctx.cash_reserve + backup_liq, ctx.monthly_expense, ctx.total_theta
            )
            embed = create_financial_runway_embed(
                cash_reserve=ctx.cash_reserve,
                monthly_expense=ctx.monthly_expense,
                total_theta=ctx.total_theta,
                runway_days=runway_days,
                backup_liquidity=backup_liq,
                extended_runway=ext_runway,
            )
        except Exception as e:
            await interaction.followup.send(
                embed=create_error_embed(f"計算跑道失敗: {e}"), ephemeral=True
            )
        finally:
            await self._reset_loading(interaction, embed=embed)

    @discord.ui.button(label="👻 VTR 績效", style=discord.ButtonStyle.secondary)
    async def btn_vtr(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ):
        await interaction.response.defer()
        await self._set_loading(interaction)
        embed = None
        try:
            from market_analysis.ghost_trader import GhostTrader
            from market_analysis.attribution import AttributionEngine

            await AttributionEngine.finalize_vtr_attribution(self.user_id)
            stats = await GhostTrader.get_vtr_performance_stats(self.user_id)
            attr_lines = AttributionEngine.format_attribution_report(self.user_id)
            embed = build_vtr_stats_embed(
                interaction.user.display_name, stats, attr_lines
            )
        except Exception as e:
            await interaction.followup.send(
                embed=create_error_embed(f"獲取 VTR 績效失敗: {e}"),
                ephemeral=True,
            )
        finally:
            await self._reset_loading(interaction, embed=embed)


class PulseHubView(discord.ui.View):
    """
    Interactive view for the Pulse Hub (/market).
    """

    def __init__(self, user_id: int, bot):
        super().__init__(timeout=300)
        self.user_id = user_id
        self.bot = bot

    async def _set_loading(self, interaction: discord.Interaction):
        for child in self.children:
            if isinstance(child, discord.ui.Button):
                child.disabled = True
        await interaction.edit_original_response(view=self)

    async def _reset_loading(self, interaction: discord.Interaction, embed=None):
        for child in self.children:
            if isinstance(child, discord.ui.Button):
                child.disabled = False
        await interaction.edit_original_response(embed=embed, view=self)

    @discord.ui.button(label="📅 市場日曆", style=discord.ButtonStyle.primary)
    async def btn_calendar(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ):
        await interaction.response.defer()
        await self._set_loading(interaction)
        embed = None
        try:
            from services.calendar_service import calendar_service

            events = await calendar_service.get_portfolio_events(self.user_id)
            embed = create_market_calendar_embed(
                events,
                max_items=15,
                empty_message="📭 未來 7 日內無影響持倉標的的重大事件或財報。",
            )
        except Exception as e:
            await interaction.followup.send(
                embed=create_error_embed(f"獲取日曆失敗: {e}"), ephemeral=True
            )
        finally:
            await self._reset_loading(interaction, embed=embed)

    @discord.ui.button(label="🐋 預測市場", style=discord.ButtonStyle.primary)
    async def btn_poly(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ):
        await interaction.response.defer()
        await self._set_loading(interaction)
        embed = None
        try:
            if not hasattr(self.bot, "polymarket_service"):
                embed = create_error_embed(
                    "Polymarket 服務未初始化。", title="系統錯誤"
                )
            else:
                markets = self.bot.polymarket_service.get_active_markets(limit=20)
                embed = create_polymarket_list_embed(markets)
        except Exception as e:
            await interaction.followup.send(
                embed=create_error_embed(f"獲取預測市場失敗: {e}"),
                ephemeral=True,
            )
        finally:
            await self._reset_loading(interaction, embed=embed)

    @discord.ui.button(label="🔥 高波動掃描", style=discord.ButtonStyle.secondary)
    async def btn_iv(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer()
        await self._set_loading(interaction)
        embed = None
        try:
            from market_analysis.volatility_inspector import VolatilityInspector

            all_watchlists = database.get_all_watchlist()
            user_watch = [row[1] for row in all_watchlists if row[0] == self.user_id]
            if not user_watch:
                embed = create_info_embed(
                    "查無資料", "📭 觀察清單為空，無法執行 IV 掃描。"
                )
            else:
                inspector = VolatilityInspector(self.bot)
                results = await inspector.run_scan(user_watch, self.user_id)
                high_iv = [
                    r
                    for r in results
                    if r.get("iv_rank", 0) > 80 or r.get("is_high_risk_vol")
                ]
                embed = create_iv_risk_scan_embed(high_iv)
        except Exception as e:
            await interaction.followup.send(
                embed=create_error_embed(f"執行 IV 掃描失敗: {e}"),
                ephemeral=True,
            )
        finally:
            await self._reset_loading(interaction, embed=embed)


class UnifiedTerminalCog(commands.Cog):
    """
    Unified Hubs for Nexus Seeker.
    Consolidates 20+ commands into 3 core hubs: /x, /dash, /market.
    """

    def __init__(self, bot):
        self.bot = bot
        logger.info("UnifiedTerminalCog loaded.")

    @app_commands.command(
        name="x", description="🌌 標體分析中心：一站式獲取報價、量化掃描與情緒分析"
    )
    @app_commands.describe(symbol="股票代號 (如 NVDA)")
    async def symbol_hub(self, interaction: discord.Interaction, symbol: str):
        await interaction.response.defer(ephemeral=True)
        symbol = symbol.upper()
        user_id = interaction.user.id

        # 🚀 Task 2 Hook: Proactive Warmup during pre-market window (08:30 - 09:30 ET)
        if hasattr(self.bot, "memory_manager"):
            coro = self.bot.memory_manager.proactive_warmup()
            if asyncio.iscoroutine(coro):
                asyncio.create_task(coro)

        if not await market_data_service.validate_symbol(symbol):
            return await interaction.followup.send(
                embed=create_error_embed(
                    f"無效的標的代號: `{symbol}`", title="輸入錯誤"
                ),
                ephemeral=True,
            )

        try:
            from services.asset_manager import AssetManager
            from models.asset import ContextType

            manager = AssetManager()
            assets = manager.get_assets(user_id, ContextType.HOLDING)
            stock_cost = next(
                (a.metadata.get("avg_cost", 0.0) for a in assets if a.symbol == symbol),
                0.0,
            )

            spy_task = market_data_service.get_spy_history_df("1y")
            macro_task = market_data_service.get_macro_environment()
            df_spy, macro_raw = await asyncio.gather(spy_task, macro_task)
            spy_price = df_spy["Close"].iloc[-1] if not df_spy.empty else 670.0
            macro_data = MacroContext(
                vix=macro_raw.get("vix", 18.0),
                oil_price=macro_raw.get("oil", 75.0),
                vix_change=macro_raw.get("vix_change", 0.0),
            )

            result = await market_math.analyze_symbol(
                symbol, stock_cost, df_spy, spy_price, vix_spot=macro_data.vix
            )
            if not result:
                result = {"symbol": symbol, "stock_cost": stock_cost, "price": 0.0}

            df_hist_1d = await market_data_service.get_history_df(
                symbol, period="1y", interval="1d"
            )
            psq_result = analyze_psq(df_hist_1d, vix_spot=macro_data.vix)
            if psq_result:
                result["psq_result"] = psq_result
                result["price"] = (
                    df_hist_1d["Close"].iloc[-1]
                    if not df_hist_1d.empty
                    else result.get("price", 0.0)
                )

            skew_data = await SentimentEngine.calculate_skew(symbol)
            result["skew"] = skew_data.get("skew", 0.0)
            result["skew_percentile"] = SentimentEngine.get_indicator_percentile(
                symbol, "SKEW", result["skew"]
            )

            result["vix"] = macro_data.vix
            result["spy_price"] = spy_price

            # 額外獲取 DDP 與情緒數據
            from market_analysis.ddp_inspector import DDPInspector

            ddp_inspector = DDPInspector(self.bot)
            ddp_report = await ddp_inspector.inspect_symbol(symbol)
            result["is_ddp"] = ddp_report.get("is_ddp", False) if ddp_report else False
            iv_metrics = await SentimentEngine.fetch_and_calculate_iv_metrics(symbol)
            result["iv_rank"] = iv_metrics.iv_rank

            max_pain_data = await SentimentEngine.calculate_max_pain(symbol)
            result["max_pain"] = max_pain_data.get("max_pain", 0.0)

            reddit_text = await reddit_service.get_reddit_context(symbol)
            # 簡單情緒判定
            if "看多" in reddit_text or "Bullish" in reddit_text:
                result["reddit_sentiment_score"] = "🚀 樂觀 (Bullish)"
            elif "看空" in reddit_text or "Bearish" in reddit_text:
                result["reddit_sentiment_score"] = "💀 恐慌 (Bearish)"
            else:
                result["reddit_sentiment_score"] = "⚖️ 中性"

            # Polymarket 數據 (簡化版：若有市場則取第一名的勝率)
            from services.polymarket_service import PolymarketService

            poly_service = PolymarketService(self.bot)
            poly_markets = await poly_service.get_market_snapshot(limit=10)
            # 尋找與該 symbol 相關的市場 (模糊匹配)
            poly_odds = "N/A"
            for m in poly_markets:
                if symbol.lower() in m.get("question", "").lower():
                    tokens = m.get("tokens", [])
                    if tokens:
                        poly_odds = f"{tokens[0].get('outcome', 'Yes')}: {float(tokens[0].get('price', 0))*100:.1f}%"
                    break
            result["polymarket_odds"] = poly_odds

            main_embed = create_tactical_symbol_embed(result)

            view = SymbolHubView(symbol, user_id, self.bot)
            view.base_data = result

            await interaction.followup.send(embed=main_embed, view=view, ephemeral=True)

        except Exception as e:
            logger.error(f"Symbol Hub Error for {symbol}: {e}")
            await interaction.followup.send(
                embed=create_error_embed(f"載入 `{symbol}` 資料時發生錯誤: {e}"),
                ephemeral=True,
            )

    @app_commands.command(
        name="dash", description="📊 交易員看板：一站式監控持倉、跑道與 VTR 績效"
    )
    async def portfolio_hub(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        user_id = interaction.user.id

        # 🚀 Task 2 Hook: Proactive Warmup during pre-market window
        if hasattr(self.bot, "memory_manager"):
            coro = self.bot.memory_manager.proactive_warmup()
            if asyncio.iscoroutine(coro):
                asyncio.create_task(coro)

        from services.trading_service import TradingService

        trading_service = TradingService(self.bot)
        pnl_data = await trading_service.get_portfolio_pnl(user_id)
        ctx = database.get_full_user_context(user_id)

        # 獲取 VIX 資訊
        macro_raw = await market_data_service.get_macro_environment()
        vix_spot = macro_raw.get("vix", 18.0)

        embed = create_strategic_dash_embed(ctx, pnl_data, vix_spot=vix_spot)

        view = PortfolioHubView(user_id, self.bot)
        await interaction.followup.send(embed=embed, view=view, ephemeral=True)

    @app_commands.command(
        name="market", description="🌌 市場情報中心：監控日曆、預測市場與高波動標的"
    )
    async def pulse_hub(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)

        # 🚀 Task 2 Hook: Proactive Warmup during pre-market window
        if hasattr(self.bot, "memory_manager"):
            coro = self.bot.memory_manager.proactive_warmup()
            if asyncio.iscoroutine(coro):
                asyncio.create_task(coro)

        from services.calendar_service import calendar_service

        events = await calendar_service.get_portfolio_events(interaction.user.id)
        embed = create_market_calendar_embed(
            events,
            max_items=10,
            empty_message="📭 未來 7 日內無重大事件。",
        )

        view = PulseHubView(interaction.user.id, self.bot)
        await interaction.followup.send(embed=embed, view=view, ephemeral=True)


async def setup(bot):
    await bot.add_cog(UnifiedTerminalCog(bot))
