from typing import Any
import discord
import asyncio
import logging
from typing import Dict

import database
from services import market_data_service, news_service, reddit_service, llm_service
from market_analysis.sentiment_engine import SentimentEngine
from market_analysis.psq_engine import analyze_psq
from market_analysis.risk_engine import MacroContext
import market_math
from cogs.embed_builder import (
    create_error_embed,
    create_media_sentiment_embed,
    create_tactical_symbol_embed,
    create_tactical_hedge_embed,
)
from .utils import find_matching_polymarket_odds

logger = logging.getLogger(__name__)


def _safe_float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value) if value is not None else default
    except (TypeError, ValueError):
        return default


class SymbolHubView(discord.ui.View):
    """
    Interactive view for the Unified Symbol Hub (/x).
    Updates the original message in-place and provides loading feedback.
    """

    def __init__(self, symbol: str, user_id: int, bot: Any):
        super().__init__(timeout=300)
        self.symbol = symbol.upper()
        self.user_id = user_id
        self.bot = bot
        self.base_data: Dict[str, Any] = {}

    async def _set_loading(self, interaction: discord.Interaction) -> Any:
        """將所有按鈕設為禁用狀態以表示讀取中"""
        for child in self.children:
            if isinstance(child, discord.ui.Button):
                child.disabled = True
        await interaction.edit_original_response(view=self)

    async def _reset_loading(
        self, interaction: discord.Interaction, embed: Any = None
    ) -> Any:
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
    ) -> Any:
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
    ) -> Any:
        await interaction.response.defer()
        await self._set_loading(interaction)
        embed = None
        try:
            news_task = news_service.fetch_recent_news(self.symbol)
            reddit_posts = self.base_data.get("reddit_posts")
            reddit_text = self.base_data.get("reddit_text")
            reddit_score = self.base_data.get("reddit_sentiment_score")
            poly_odds = self.base_data.get("polymarket_odds")

            if not reddit_text and not reddit_posts:
                news_text, (reddit_text, reddit_posts) = await asyncio.gather(
                    news_task, reddit_service.get_reddit_details(self.symbol)
                )
            else:
                news_text = await news_task

            embed = create_media_sentiment_embed(
                self.symbol,
                news_text=news_text,
                reddit_text=reddit_text,
                polymarket_odds=poly_odds,
                reddit_posts=reddit_posts,
                reddit_sentiment_score=reddit_score,
            )
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
    ) -> Any:
        await interaction.response.defer()
        await self._set_loading(interaction)
        embed = None
        try:
            # 清除該 symbol 在 sentiment_engine 中的 BoundedCache 快取
            from market_analysis.sentiment_engine import _iv_cache

            if self.symbol in _iv_cache:
                del _iv_cache[self.symbol]
                logger.info(f"[{self.symbol}] 按鈕觸發：已清除 IV 數據快取")

            from market_analysis.intraday_pipeline import _WATCHLIST_METRICS_CACHE

            if self.symbol in _WATCHLIST_METRICS_CACHE:
                del _WATCHLIST_METRICS_CACHE[self.symbol]

            from services.market_data_service import _quote_cache, _history_cache

            if self.symbol in _quote_cache:
                del _quote_cache[self.symbol]
            keys_to_delete = [k for k in _history_cache if k[0] == self.symbol]
            for k in keys_to_delete:
                del _history_cache[k]

            from database import mark_market_cache_stale
            import asyncio

            await asyncio.to_thread(mark_market_cache_stale, self.symbol)

            # 獲取 stock_cost
            from services.asset_manager import AssetManager
            from models.asset import ContextType

            manager = AssetManager()
            assets = manager.get_assets(self.user_id, ContextType.HOLDING)
            stock_cost_raw = next(
                (
                    a.metadata.get("avg_cost", 0.0)
                    for a in assets
                    if a.symbol == self.symbol
                ),
                0.0,
            )
            stock_cost = _safe_float(stock_cost_raw, 0.0)

            # 用於 DDP 與 Polymarket 等服務
            from market_analysis.ddp_inspector import DDPInspector

            ddp_inspector = DDPInspector(self.bot)
            poly_service = getattr(self.bot, "polymarket_service", None)

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
            reddit_task = reddit_service.get_reddit_details(self.symbol)
            poly_task = (
                poly_service.get_market_snapshot(limit=0)
                if poly_service
                else asyncio.sleep(0, result=[])
            )

            ddp_task = ddp_inspector.inspect_symbol(self.symbol)
            from services.calendar_service import calendar_service

            catalyst_task = calendar_service.get_symbol_catalysts(self.symbol, days=14)
            df_hist_task = market_data_service.get_history_df(
                self.symbol, period="1y", interval="1d"
            )
            from market_analysis.volume_profile import calculate_volume_profile
            from market_analysis.dark_pool_engine import fetch_darkpool_prints
            from market_analysis.index_microstructure import fetch_symbol_gex_metrics

            vp_task = asyncio.to_thread(calculate_volume_profile, self.symbol)
            dp_task = fetch_darkpool_prints(self.symbol)
            gex_task = fetch_symbol_gex_metrics(self.symbol)

            (
                df_spy,
                macro_raw,
                quote,
                skew_data,
                pcr_data,
                uoa_data,
                max_pain_data,
                iv_metrics,
                reddit_details,
                poly_markets,
                ddp_report,
                df_hist_1d,
                catalysts,
                vp_data,
                dp_data,
                gex_data,
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
                dp_task,
                gex_task,
            )

            spy_price = _safe_float(
                (df_spy["Close"].iloc[-1] if not df_spy.empty else 670.0),
                670.0,
            )
            safe_macro = macro_raw or {}
            macro_data = MacroContext(
                vix=_safe_float(safe_macro.get("vix"), 18.0),
                oil_price=_safe_float(safe_macro.get("oil"), 75.0),
                vix_change=_safe_float(safe_macro.get("vix_change"), 0.0),
            )

            result = await market_math.analyze_symbol(
                self.symbol, stock_cost, df_spy, spy_price, vix_spot=macro_data.vix
            )
            if not isinstance(result, dict) or not result:
                result = {"symbol": self.symbol, "stock_cost": stock_cost, "price": 0.0}

            psq_result = analyze_psq(df_hist_1d, vix_spot=macro_data.vix)
            if psq_result:
                result["psq_result"] = psq_result
                is_df_valid = df_hist_1d is not None and not df_hist_1d.empty
                result["price"] = (
                    _safe_float(df_hist_1d["Close"].iloc[-1], 0.0)
                    if is_df_valid
                    else _safe_float(result.get("price"), 0.0)
                )

            result["quote"] = quote

            safe_skew = skew_data if isinstance(skew_data, dict) else {}
            result["skew"] = _safe_float(safe_skew.get("skew"), 0.0)
            result["skew_percentile"] = SentimentEngine.get_indicator_percentile(
                self.symbol, "SKEW", result["skew"]
            )

            result["pcr"] = pcr_data if pcr_data is not None else {}
            result["uoa"] = uoa_data if uoa_data is not None else []

            result["iv_data"] = iv_metrics
            iv_rank_raw = (
                iv_metrics.get("iv_rank")
                if isinstance(iv_metrics, dict)
                else getattr(iv_metrics, "iv_rank", None)
            )
            result["iv_rank"] = _safe_float(iv_rank_raw, 0.0)
            raw_em_context = await SentimentEngine.get_expected_move(
                self.symbol, quote=quote, iv_metrics=iv_metrics
            )
            result["expected_move_context"] = (
                raw_em_context if isinstance(raw_em_context, dict) else {}
            )

            safe_mp = max_pain_data if isinstance(max_pain_data, dict) else {}
            result["max_pain"] = _safe_float(safe_mp.get("max_pain"), 0.0)

            result["gex_profile_data"] = gex_data
            safe_ddp = ddp_report if isinstance(ddp_report, dict) else {}
            result["is_ddp"] = bool(safe_ddp.get("is_ddp", False))
            result["vix"] = macro_data.vix
            result["spy_price"] = spy_price

            # Reddit sentiment score & structured posts
            if isinstance(reddit_details, tuple):
                safe_reddit_text = reddit_details[0] or ""
                reddit_posts = (
                    reddit_details[1] if isinstance(reddit_details[1], list) else []
                )
            else:
                safe_reddit_text = reddit_details or ""
                reddit_posts = []

            result[
                "reddit_sentiment_score"
            ] = await llm_service.evaluate_reddit_sentiment(
                self.symbol, safe_reddit_text
            )
            result["reddit_text"] = safe_reddit_text
            result["reddit_posts"] = reddit_posts

            # Polymarket odds
            poly_odds = await find_matching_polymarket_odds(
                self.symbol, poly_markets, bot=self.bot
            )
            result["polymarket_odds"] = poly_odds

            result["catalysts"] = catalysts
            safe_vp = vp_data if isinstance(vp_data, dict) else {}
            safe_dp = dp_data if isinstance(dp_data, dict) else {}
            result["volume_profile"] = safe_vp

            # TDP 估值三擊判斷: 現價 < EMA 21 且 現價 < Max Pain 且 現價 < V-POC 且 現價 < DP-POC
            ema_21 = (
                df_hist_1d["Close"].ewm(span=21, adjust=False).mean().iloc[-1]
                if df_hist_1d is not None and not df_hist_1d.empty
                else 0.0
            )
            vpoc = _safe_float(safe_vp.get("hvn"), 0.0)
            dp_poc = _safe_float(safe_dp.get("dp_poc"), 0.0)
            max_pain = _safe_float(result["max_pain"], 0.0)
            price = _safe_float(result["price"], 0.0)

            if result.get("is_ddp"):
                if (
                    price > 0
                    and ema_21 > 0
                    and max_pain > 0
                    and vpoc > 0
                    and dp_poc > 0
                ):
                    if (
                        price < ema_21
                        and price < max_pain
                        and price < vpoc
                        and price < dp_poc
                    ):
                        result["is_ddp"] = True
                        result["tdp_activated"] = True

                        psq_res = result.get("psq_result", {})
                        is_sqz = (
                            psq_res.get("is_squeezing", False)
                            if isinstance(psq_res, dict)
                            else getattr(psq_res, "is_squeezing", False)
                        )
                        if is_sqz:
                            result["tdpq_activated"] = True

            result["darkpool"] = safe_dp

            from market_analysis.risk_engine import optimize_position_risk

            raw_stock_iv = (
                iv_metrics.get("current_iv")
                if isinstance(iv_metrics, dict)
                else getattr(iv_metrics, "current_iv", None)
            )
            stock_iv_val = _safe_float(raw_stock_iv, 0.0)
            stock_iv = stock_iv_val if stock_iv_val > 0 else 0.40
            vol_pcr = (
                _safe_float(pcr_data.get("volume_pcr"), 0.8)
                if isinstance(pcr_data, dict)
                else 0.8
            )
            skew_val = _safe_float(safe_skew.get("skew"), 0.0)

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
    ) -> Any:
        await interaction.response.defer()
        await self._set_loading(interaction)
        try:
            # 根據目前波動率與情緒自動引導對沖操作
            ivr = _safe_float(self.base_data.get("iv_rank"), 50.0)
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


class WatchlistHeartbeatView(discord.ui.View):
    """
    附掛在 Watchlist Heartbeat 訊息下方的 View，包含執行標的分析中心的按鈕。
    """

    def __init__(self, symbol: str) -> None:
        super().__init__(timeout=86400)
        self.symbol = symbol

    @discord.ui.button(
        label="標的分析中心", style=discord.ButtonStyle.primary, emoji="🌌"
    )
    async def analyze_button(
        self, interaction: discord.Interaction, button: discord.ui.Button[Any]
    ) -> None:
        button.disabled = True
        await interaction.response.edit_message(view=self)
        try:
            cog = interaction.client.get_cog("UnifiedTerminalCog")  # type: ignore
            if cog and hasattr(cog, "_run_single_symbol_hub"):
                # 呼叫 UnifiedTerminalCog 執行標的深度分析
                await getattr(cog, "_run_single_symbol_hub")(
                    interaction, self.symbol, interaction.user.id
                )
            else:
                from cogs.embed_builder import create_error_embed

                await interaction.followup.send(
                    embed=create_error_embed(
                        "無法找到終端模組 (UnifiedTerminalCog) 或方法遺失。"
                    ),
                    ephemeral=True,
                )
        finally:
            button.disabled = False
            try:
                await interaction.edit_original_response(view=self)
            except Exception:
                pass
