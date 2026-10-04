from typing import Any
import discord
import asyncio
import logging
import math
from typing import Dict, Optional

import database
from services import news_service, reddit_service
from cogs.embed_builder import (
    create_error_embed,
    create_media_sentiment_embed,
    create_tactical_symbol_embed,
    create_tactical_hedge_embed,
    create_entry_rules_embed,
    create_squeeze_entry_embed,
)

logger = logging.getLogger(__name__)


def _safe_float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value) if value is not None else default
    except (TypeError, ValueError):
        return default


def _to_float_or_none(value: Any) -> Optional[float]:
    """數值化；None、非數值字串（如 "--"）與 NaN 一律視為未知回傳 None。"""
    if value is None or isinstance(value, bool):
        return None
    try:
        f = float(value)
    except (TypeError, ValueError):
        return None
    return None if math.isnan(f) else f


_HEDGE_SELL_PUT_SPREAD = "Bull Put Spread (賣出認沽價差策略)"
_HEDGE_BUY_PROTECTION = "Bear Debits / Put Protection (買入保護性認沽)"


def _is_negative_gamma_zone(base_data: Dict[str, Any]) -> bool:
    """做市商負 Gamma 泥淖：Net GEX < 0 或現價跌穿 Put Wall。

    定義與批次雷達 (`market_embeds.build_radar_scan_embed` 的 `is_neg_gamma`) 一致，
    對應 docs/valuation_pricing/04 §1.2 的賣方一票否決條件。
    """
    gex = base_data.get("gex_profile_data")
    if not isinstance(gex, dict):
        return False
    net_gex = _to_float_or_none(gex.get("net_gex"))
    if net_gex is not None and net_gex < 0:
        return True
    put_wall = _to_float_or_none(gex.get("put_wall")) or 0.0
    quote = base_data.get("quote")
    spot_raw = quote.get("c") if isinstance(quote, dict) else None
    spot = _to_float_or_none(spot_raw)
    if spot is None or spot <= 0:
        spot = _to_float_or_none(base_data.get("price")) or 0.0
    return put_wall > 0 and spot > 0 and spot < put_wall


def _recommend_hedge_strategy(ivr: Optional[float], negative_gamma: bool) -> str:
    """一鍵對沖的策略引導。

    賣方信用價差（做空波動率）只在 IVR 已知且 > 50% 並且不在負 Gamma 泥淖時
    才推薦；負 Gamma 環境依 docs/valuation_pricing/04 §1.2「無論 IVR 多高一律
    賣方禁售」，IVR 未知時亦保守地退回買方保護。
    """
    if ivr is not None and ivr > 50.0 and not negative_gamma:
        return _HEDGE_SELL_PUT_SPREAD
    return _HEDGE_BUY_PROTECTION


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
            logger.exception(f"[{self.symbol}] Home tab render failed: {e}")
            await interaction.followup.send(
                embed=create_error_embed("恢復主頁失敗，請稍後再試。"), ephemeral=True
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
            news_task = news_service.fetch_recent_news_structured(self.symbol)
            reddit_posts = self.base_data.get("reddit_posts")
            reddit_text = self.base_data.get("reddit_text")
            reddit_score = self.base_data.get("reddit_sentiment_score")
            poly_odds = self.base_data.get("polymarket_odds")
            poly_summary = self.base_data.get("polymarket_summary")
            skew_val = (
                _safe_float(self.base_data.get("skew"))
                if self.base_data.get("skew") is not None
                else None
            )
            skew_percentile = (
                _safe_float(self.base_data.get("skew_percentile"))
                if self.base_data.get("skew_percentile") is not None
                else None
            )

            raw_pcr = self.base_data.get("pcr")
            pcr_dict = raw_pcr if isinstance(raw_pcr, dict) else {}
            pcr_raw_val = pcr_dict.get("volume_pcr", pcr_dict.get("pcr"))
            pcr_val = _safe_float(pcr_raw_val) if pcr_raw_val is not None else None

            if not reddit_text and not reddit_posts:
                news_items, (reddit_text, reddit_posts) = await asyncio.gather(
                    news_task, reddit_service.get_reddit_details(self.symbol)
                )
            else:
                news_items = await news_task

            embed = create_media_sentiment_embed(
                self.symbol,
                news_items=news_items,
                reddit_text=reddit_text,
                polymarket_odds=poly_odds,
                polymarket_summary=poly_summary,
                reddit_posts=reddit_posts,
                reddit_sentiment_score=reddit_score,
                skew_val=skew_val,
                skew_percentile=skew_percentile,
                pcr_val=pcr_val,
            )
        except Exception as e:
            logger.exception(f"[{self.symbol}] Media tab failed: {e}")
            await interaction.followup.send(
                embed=create_error_embed("獲取輿情社群失敗，請稍後再試。"),
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

            await mark_market_cache_stale(self.symbol)

            cog = self.bot.get_cog("UnifiedTerminalCog") if self.bot else None
            if (
                cog
                and hasattr(cog, "_fetch_single_symbol_data_raw")
                and hasattr(cog, "_process_symbol_hub_data")
            ):
                raw_data = await cog._fetch_single_symbol_data_raw(self.symbol)
                result = await cog._process_symbol_hub_data(
                    self.symbol, self.user_id, raw_data
                )
            else:
                from cogs.unified_terminal.symbol_deep_dive import SymbolDeepDiveMixin

                class _HelperDeepDive(SymbolDeepDiveMixin):
                    def __init__(self, bot: Any) -> None:
                        self.bot = bot

                helper = _HelperDeepDive(self.bot)
                raw_data = await helper._fetch_single_symbol_data_raw(self.symbol)
                result = await helper._process_symbol_hub_data(
                    self.symbol, self.user_id, raw_data
                )

            self.base_data = result
            embed = create_tactical_symbol_embed(self.base_data)
        except Exception as e:
            logger.exception(f"[{self.symbol}] Refresh failed: {e}")
            await interaction.followup.send(
                embed=create_error_embed("重整數據失敗，請稍後再試。"), ephemeral=True
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
        embed = None
        try:
            # 根據目前波動率與做市商 Gamma 環境引導對沖操作。
            # IVR 未知（樣本不足/非數值）時維持 None：舊實作補 50.0 會在畫面上
            # 捏造一個 IVR 讀數。
            ivr = _to_float_or_none(self.base_data.get("iv_rank"))
            rec_strategy = _recommend_hedge_strategy(
                ivr, _is_negative_gamma_zone(self.base_data)
            )

            embed = create_tactical_hedge_embed(self.symbol, ivr, rec_strategy)
        except Exception as e:
            logger.exception(f"[{self.symbol}] Hedge tab failed: {e}")
            await interaction.followup.send(
                embed=create_error_embed("開啟對沖中心失敗，請稍後再試。"),
                ephemeral=True,
            )
        finally:
            await self._reset_loading(interaction, embed=embed)

    @discord.ui.button(
        label="🔐 進場檢核",
        style=discord.ButtonStyle.secondary,
        custom_id="btn_entry_rules",
        row=1,
    )
    async def btn_entry_rules(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> Any:
        """
        進場檢核頁籤：依使用者 /settings 選擇的交易策略模式呈現對應判定。

        * 多頭建倉（右側交易，以及動態調整中非左側、非做空的 Regime）：多時間框架
          擠壓規則（`intraday_pipeline/entry_advisor.py::_evaluate_squeeze_long`，
          與進場顧問推播同一份判定）。
        * 左側交易／動態調整 Regime I：左側六重鐵律。
        * 做空交易／動態調整 Regime V：做空六重鐵律。
        """
        await interaction.response.defer()
        await self._set_loading(interaction)
        embed = None
        from market_analysis import evaluation_recorder

        # 前向蒐集：使用者手動檢核同樣是一次真實評估，標記來源後於回應送出
        # 「之後」才批次寫入，不拖慢互動回應。
        eval_source_token = evaluation_recorder.set_evaluation_source("SYMBOL_VIEW")
        try:
            from market_analysis.dynamic_rollover.models import (
                DynamicRegime,
                TradingStrategyMode,
            )
            from market_analysis.intraday_pipeline.entry_advisor import (
                EntryAdvice,
                _evaluate_squeeze_long,
            )

            _quote = self.base_data.get("quote") or {}
            _c_raw = (
                _quote.get("c")
                if _quote.get("c") is not None
                else self.base_data.get("price")
            )
            target_spot = _safe_float(_c_raw, 0.0)

            try:
                trading_strategy = database.get_full_user_context(
                    self.user_id
                ).trading_strategy
            except Exception as e:
                trading_strategy = TradingStrategyMode.RIGHT_SIDE.value
                logger.warning(
                    f"[{self.symbol}] 讀取使用者 {self.user_id} 交易策略設定失敗，"
                    f"退回右側交易預設: {e}"
                )

            dynamic_regime: Any = None
            squeeze_advice: Optional[EntryAdvice] = None
            dynamic_regime_reason = None
            # 兩套鐵律的入口簽章現已完全收斂，皆回傳 (是否通過, 原因, 建議結構)。
            structure_directive: Optional[str] = None

            if trading_strategy == TradingStrategyMode.LEFT_SIDE.value:
                from market_analysis.dynamic_rollover.left_side_entry import (
                    _confirm_left_entry_signal,
                )

                (
                    six_rule_passed,
                    six_rule_reason,
                    structure_directive,
                ) = await _confirm_left_entry_signal(
                    self.symbol, self.base_data, target_spot
                )
            elif trading_strategy == TradingStrategyMode.SHORT_SIDE.value:
                from market_analysis.dynamic_rollover.short_side_entry import (
                    _confirm_short_entry_signal,
                )

                (
                    six_rule_passed,
                    six_rule_reason,
                    structure_directive,
                ) = await _confirm_short_entry_signal(
                    self.symbol, self.base_data, target_spot
                )
            elif trading_strategy == TradingStrategyMode.DYNAMIC.value:
                from market_analysis.dynamic_rollover.left_side_entry import (
                    _confirm_left_entry_signal,
                )
                from market_analysis.dynamic_rollover.regime_classifier import (
                    classify_dynamic_regime,
                )
                from market_analysis.dynamic_rollover.short_side_entry import (
                    _confirm_short_entry_signal,
                )

                gex_profile_data = self.base_data.get("gex_profile_data") or {}
                uoa_list = self.base_data.get("uoa") or []
                (
                    dynamic_regime,
                    dynamic_regime_reason,
                    regime_market_data,
                ) = await classify_dynamic_regime(
                    self.symbol, target_spot, gex_profile_data, uoa_list
                )
                if dynamic_regime == DynamicRegime.REGIME_I_LEFT_CATCH:
                    (
                        six_rule_passed,
                        six_rule_reason,
                        structure_directive,
                    ) = await _confirm_left_entry_signal(
                        self.symbol,
                        self.base_data,
                        target_spot,
                        df_15m=regime_market_data.df_15m,
                        session_vwap=regime_market_data.session_vwap,
                        atr_15m=regime_market_data.atr_15m,
                    )
                elif dynamic_regime == DynamicRegime.REGIME_V_BREAKDOWN_CHASE:
                    (
                        six_rule_passed,
                        six_rule_reason,
                        structure_directive,
                    ) = await _confirm_short_entry_signal(
                        self.symbol,
                        self.base_data,
                        target_spot,
                        df_15m=regime_market_data.df_15m,
                        session_vwap=regime_market_data.session_vwap,
                        atr_15m=regime_market_data.atr_15m,
                    )
                else:
                    squeeze_advice = await _evaluate_squeeze_long(
                        trading_strategy,
                        self.symbol,
                        target_spot,
                        dynamic_regime.value,
                        dynamic_regime_reason,
                    )
            else:
                squeeze_advice = await _evaluate_squeeze_long(
                    trading_strategy, self.symbol, target_spot
                )

            if squeeze_advice is not None:
                embed = create_squeeze_entry_embed(
                    self.symbol,
                    squeeze_advice.squeeze,
                    passed=squeeze_advice.passed,
                    reason=squeeze_advice.reason,
                    entry_price=squeeze_advice.entry_price,
                    stop_loss=squeeze_advice.stop_loss,
                    trading_strategy=trading_strategy,
                    dynamic_regime=squeeze_advice.regime,
                )
                return

            six_rule_reasons = six_rule_reason.split(" | ") if six_rule_reason else []

            embed = create_entry_rules_embed(
                self.symbol,
                six_rule_passed,
                six_rule_reasons,
                trading_strategy=trading_strategy,
                dynamic_regime=(
                    dynamic_regime.value if dynamic_regime is not None else None
                ),
                dynamic_regime_reason=dynamic_regime_reason,
                structure_directive=structure_directive,
            )
        except Exception as e:
            logger.exception(f"[{self.symbol}] Entry rules check failed: {e}")
            await interaction.followup.send(
                embed=create_error_embed("進場檢核失敗，請稍後再試。"),
                ephemeral=True,
            )
        finally:
            evaluation_recorder.reset_evaluation_source(eval_source_token)
            await self._reset_loading(interaction, embed=embed)
            await evaluation_recorder.flush_evaluations()


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
