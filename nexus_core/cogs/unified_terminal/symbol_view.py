import discord
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
    create_info_embed,
    create_media_sentiment_embed,
    create_tactical_symbol_embed,
    create_tactical_hedge_embed,
)
from .utils import find_matching_polymarket_odds

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
            ctx = database.get_full_user_context(self.user_id)
            reddit_task = reddit_service.get_reddit_context(
                self.symbol, enable_tunnel=ctx.enable_local_tunnel
            )
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

            # 用於 DDP 與 Polymarket 等服務
            from market_analysis.ddp_inspector import DDPInspector
            from services.polymarket_service import PolymarketService

            ddp_inspector = DDPInspector(self.bot)
            poly_service = PolymarketService(self.bot)

            # 並行抓取所有數據
            spy_task = market_data_service.get_spy_history_df("1y")
            macro_task = market_data_service.get_macro_environment()
            quote_task = market_data_service.get_quote(self.symbol)
            skew_task = SentimentEngine.calculate_skew(self.symbol)
            pcr_task = SentimentEngine.calculate_pcr(self.symbol)
            uoa_task = SentimentEngine.detect_uoa(self.symbol)
            mp_task = SentimentEngine.calculate_max_pain(self.symbol)
            iv_task = SentimentEngine.fetch_and_calculate_iv_metrics(self.symbol)
            ctx = database.get_full_user_context(self.user_id)
            reddit_task = reddit_service.get_reddit_context(
                self.symbol, enable_tunnel=ctx.enable_local_tunnel
            )
            poly_task = poly_service.get_market_snapshot(limit=10)
            ddp_task = ddp_inspector.inspect_symbol(self.symbol)
            from services.calendar_service import calendar_service

            catalyst_task = calendar_service.get_symbol_catalysts(self.symbol, days=14)
            df_hist_task = market_data_service.get_history_df(
                self.symbol, period="1y", interval="1d"
            )
            from market_analysis.volume_profile import calculate_volume_profile

            vp_task = asyncio.to_thread(calculate_volume_profile, self.symbol)

            (
                df_spy,
                macro_raw,
                quote,
                skew_data,
                pcr_data,
                uoa_data,
                max_pain_data,
                iv_metrics,
                reddit_text,
                poly_markets,
                ddp_report,
                df_hist_1d,
                catalysts,
                vp_data,
            ) = await asyncio.gather(
                spy_task,
                macro_task,
                quote_task,
                skew_task,
                pcr_task,
                uoa_task,
                mp_task,
                iv_task,
                reddit_task,
                poly_task,
                ddp_task,
                df_hist_task,
                catalyst_task,
                vp_task,
            )

            spy_price = df_spy["Close"].iloc[-1] if not df_spy.empty else 670.0
            safe_macro = macro_raw or {}
            macro_data = MacroContext(
                vix=safe_macro.get("vix", 18.0),
                oil_price=safe_macro.get("oil", 75.0),
                vix_change=safe_macro.get("vix_change", 0.0),
            )

            result = await market_math.analyze_symbol(
                self.symbol, stock_cost, df_spy, spy_price, vix_spot=macro_data.vix
            )
            if not result:
                result = {"symbol": self.symbol, "stock_cost": stock_cost, "price": 0.0}

            psq_result = analyze_psq(df_hist_1d, vix_spot=macro_data.vix)
            if psq_result:
                result["psq_result"] = psq_result
                is_df_valid = df_hist_1d is not None and not df_hist_1d.empty
                result["price"] = (
                    df_hist_1d["Close"].iloc[-1]
                    if is_df_valid
                    else result.get("price", 0.0)
                )

            result["quote"] = quote

            safe_skew = skew_data or {}
            result["skew"] = safe_skew.get("skew", 0.0)
            result["skew_percentile"] = SentimentEngine.get_indicator_percentile(
                self.symbol, "SKEW", result["skew"]
            )

            result["pcr"] = pcr_data if pcr_data is not None else {}
            result["uoa"] = uoa_data if uoa_data is not None else []

            result["iv_data"] = iv_metrics
            result["iv_rank"] = iv_metrics.iv_rank if iv_metrics else 0.0

            safe_mp = max_pain_data or {}
            result["max_pain"] = safe_mp.get("max_pain", 0.0)

            result["is_ddp"] = ddp_report.get("is_ddp", False) if ddp_report else False
            result["vix"] = macro_data.vix
            result["spy_price"] = spy_price

            # Reddit sentiment score
            safe_reddit_text = reddit_text or ""
            if "看多" in safe_reddit_text or "Bullish" in safe_reddit_text:
                result["reddit_sentiment_score"] = "🚀 樂觀 (Bullish)"
            elif "看空" in safe_reddit_text or "Bearish" in safe_reddit_text:
                result["reddit_sentiment_score"] = "💀 恐慌 (Bearish)"
            else:
                result["reddit_sentiment_score"] = "⚖️ 中性"

            # Polymarket odds
            poly_odds = find_matching_polymarket_odds(self.symbol, poly_markets)
            result["polymarket_odds"] = poly_odds
            result["catalysts"] = catalysts
            result["volume_profile"] = vp_data

            from market_analysis.risk_engine import optimize_position_risk

            stock_iv = (
                iv_metrics.current_iv
                if iv_metrics
                and hasattr(iv_metrics, "current_iv")
                and iv_metrics.current_iv
                else 0.40
            )
            vol_pcr = (
                float(pcr_data.get("volume_pcr", 0.8))
                if isinstance(pcr_data, dict) and pcr_data.get("volume_pcr")
                else 0.8
            )
            skew_val = float(safe_skew.get("skew", 0.0))

            opt_result = optimize_position_risk(
                current_delta=0.0,
                unit_weighted_delta=0.16,
                user_capital=ctx.capital,
                spy_price=spy_price,
                stock_iv=stock_iv,
                strategy="STO",
                macro_data=macro_data,
                risk_limit=ctx.risk_limit,
                vix_spot=macro_data.vix,
                pcr=vol_pcr,
                skew=skew_val,
            )
            result["kelly_sizing"] = opt_result

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
