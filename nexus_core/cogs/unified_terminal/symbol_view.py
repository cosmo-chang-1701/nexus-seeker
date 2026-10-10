from typing import Any
import discord
import asyncio
import logging
import math
from dataclasses import dataclass
from typing import Dict, List, Optional

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
        self._apply_button_states()
        # 守衛：embed=None 時不傳 embed 參數，避免按鈕失敗時把原畫面清空
        # （比照 PulseHubView）；成功路徑一律帶 embed，行為不變。
        kwargs: Dict[str, Any] = {"view": self}
        if embed is not None:
            kwargs["embed"] = embed
        await interaction.edit_original_response(**kwargs)

    def _apply_button_states(self) -> None:
        """讀取結束後套用按鈕可用狀態的鉤子；預設全部啟用，子類別可覆寫。"""
        for child in self.children:
            if isinstance(child, discord.ui.Button):
                child.disabled = False

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


# SymbolHubView 五顆功能按鈕的 custom_id（換頁 View 以此判斷是否需標記忙碌）
_HUB_FEATURE_CUSTOM_IDS = frozenset(
    {"btn_home", "btn_media", "btn_refresh", "btn_hedge", "btn_entry_rules"}
)


@dataclass
class SymbolHubPage:
    """多標的換頁中的單一頁面。`base_data` 為 None 代表該標的載入失敗（錯誤頁）。"""

    symbol: str
    base_data: Optional[Dict[str, Any]]
    embed: discord.Embed


class SymbolBatchHubView(SymbolHubView):
    """/x 多標的深度分析：單則訊息逐檔換頁。

    一檔 tactical embed 已逼近 6000 字上限，無法一則訊息放多檔；逐檔 followup.send
    又會撞 Discord 40094（見 BatchScanPaginatedView），因此沿用 SymbolHubView 的
    五顆功能按鈕，換頁時切換 `symbol`／`base_data` 即可重用所有 callback。
    """

    def __init__(
        self,
        pages: List[SymbolHubPage],
        user_id: int,
        bot: Any,
        start_index: int = 0,
    ) -> None:
        if not pages:
            raise ValueError("SymbolBatchHubView 至少需要一頁")
        super().__init__(pages[0].symbol, user_id, bot)
        self.pages = pages
        self.index = min(max(start_index, 0), len(pages) - 1)
        # 功能按鈕（含即時整理）執行中不得換頁，否則結果會寫到別頁
        self._busy = False
        self.last_interaction: Optional[discord.Interaction] = None

        self.btn_prev: discord.ui.Button[Any] = discord.ui.Button(
            label="◀ 上一檔",
            style=discord.ButtonStyle.secondary,
            custom_id="btn_batch_prev",
            row=2,
        )
        self.btn_pos: discord.ui.Button[Any] = discord.ui.Button(
            label="",
            style=discord.ButtonStyle.secondary,
            custom_id="btn_batch_pos",
            disabled=True,
            row=2,
        )
        self.btn_next: discord.ui.Button[Any] = discord.ui.Button(
            label="下一檔 ▶",
            style=discord.ButtonStyle.secondary,
            custom_id="btn_batch_next",
            row=2,
        )
        self.btn_prev.callback = self._on_prev  # type: ignore[method-assign]
        self.btn_next.callback = self._on_next  # type: ignore[method-assign]
        self.add_item(self.btn_prev)
        self.add_item(self.btn_pos)
        self.add_item(self.btn_next)

        self._select_page(self.index)
        self._apply_button_states()

    def _select_page(self, index: int) -> None:
        """切換目前頁，並同步 SymbolHubView 各 callback 讀取的 symbol／base_data。"""
        self.index = index
        page = self.pages[index]
        self.symbol = page.symbol
        self.base_data = page.base_data if page.base_data is not None else {}

    def current_embed(self) -> discord.Embed:
        """成功頁以最新 base_data（含即時整理後的資料）重建 embed，失敗退回快取。"""
        page = self.pages[self.index]
        if page.base_data is None:
            return page.embed
        try:
            return create_tactical_symbol_embed(self.base_data)
        except Exception as e:
            logger.exception(f"[{page.symbol}] 換頁重建 embed 失敗，退回快取: {e}")
            return page.embed

    def _apply_button_states(self) -> None:
        page = self.pages[self.index]
        is_error_page = page.base_data is None
        for child in self.children:
            if isinstance(child, discord.ui.Button):
                child.disabled = is_error_page
        self.btn_prev.disabled = self.index <= 0
        self.btn_next.disabled = self.index >= len(self.pages) - 1
        self.btn_pos.disabled = True
        prefix = "⚠️ " if is_error_page else ""
        self.btn_pos.label = (
            f"{prefix}{self.index + 1}/{len(self.pages)} · {page.symbol}"
        )

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        """功能按鈕在 callback 開頭就先 defer，到 _set_loading 才停用按鈕；
        若等到 _set_loading 才標記忙碌，這段空窗內換頁會讓功能按鈕讀到別頁的
        symbol／base_data。因此在派送 callback 前就標記忙碌。"""
        data: dict[str, Any] = (
            dict(interaction.data) if isinstance(interaction.data, dict) else {}
        )
        if data.get("custom_id") in _HUB_FEATURE_CUSTOM_IDS:
            if self._busy:
                # 上一個功能按鈕仍在執行：僅確認互動，不重複執行
                await interaction.response.defer()
                return False
            self._busy = True
        return True

    async def on_error(
        self,
        interaction: discord.Interaction,
        error: Exception,
        item: discord.ui.Item[Any],
    ) -> None:
        # callback 在 _reset_loading 之前就拋出例外時，不可讓換頁永久鎖死
        self._busy = False
        await super().on_error(interaction, error, item)

    async def _set_loading(self, interaction: discord.Interaction) -> Any:
        self.last_interaction = interaction
        self._busy = True
        try:
            return await super()._set_loading(interaction)
        except Exception:
            self._busy = False
            raise

    async def _reset_loading(
        self, interaction: discord.Interaction, embed: Any = None
    ) -> Any:
        self.last_interaction = interaction
        try:
            return await super()._reset_loading(interaction, embed=embed)
        finally:
            self._busy = False

    async def _goto(self, interaction: discord.Interaction, new_index: int) -> None:
        if self._busy:
            # 功能按鈕仍在執行：僅確認互動，避免「此互動失敗」，不動頁面
            await interaction.response.defer()
            return
        if not 0 <= new_index < len(self.pages):
            await interaction.response.defer()
            return
        # 保留即時整理後的新資料；錯誤頁的 {} 佔位不可寫回而變成成功頁
        current = self.pages[self.index]
        if current.base_data is not None:
            current.base_data = self.base_data
        self._select_page(new_index)
        self._apply_button_states()
        self.last_interaction = interaction
        await interaction.response.edit_message(embed=self.current_embed(), view=self)

    async def _on_prev(self, interaction: discord.Interaction) -> None:
        await self._goto(interaction, self.index - 1)

    async def _on_next(self, interaction: discord.Interaction) -> None:
        await self._goto(interaction, self.index + 1)

    async def on_timeout(self) -> None:
        """逾時後移除按鈕，避免殘留點了只會顯示「此互動失敗」的殭屍按鈕。"""
        if self.last_interaction is None:
            return
        try:
            await self.last_interaction.edit_original_response(view=None)
        except Exception as e:
            logger.debug(f"SymbolBatchHubView 逾時移除按鈕失敗: {e}")


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
