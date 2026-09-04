from typing import Any
import discord
import asyncio
import logging
from typing import Dict

from services import news_service, reddit_service
from cogs.embed_builder import (
    create_error_embed,
    create_media_sentiment_embed,
    create_tactical_symbol_embed,
    create_tactical_hedge_embed,
    create_entry_rules_embed,
)

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
        embed = None
        try:
            # 根據目前波動率與情緒自動引導對沖操作
            ivr = _safe_float(self.base_data.get("iv_rank"), 50.0)
            rec_strategy = (
                "Bull Put Spread (賣出認沽價差策略)"
                if ivr > 50.0
                else "Bear Debits / Put Protection (買入保護性認沽)"
            )

            embed = create_tactical_hedge_embed(self.symbol, ivr, rec_strategy)
        except Exception as e:
            await interaction.followup.send(
                embed=create_error_embed(f"開啟對沖中心失敗: {e}"), ephemeral=True
            )
        finally:
            await self._reset_loading(interaction, embed=embed)

    @discord.ui.button(
        label="🔐 進場鐵律檢核",
        style=discord.ButtonStyle.secondary,
        custom_id="btn_entry_rules",
        row=1,
    )
    async def btn_entry_rules(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> Any:
        """
        進場鐵律檢核頁籤：呈現六重鐵律 (dynamic_rollover/opportunity_cost.py::
        _confirm_entry_signal，本專案既有機會成本轉倉候選確認生產路徑，含即時
        I/O) 的判定結果。
        """
        await interaction.response.defer()
        await self._set_loading(interaction)
        embed = None
        try:
            from market_analysis.dynamic_rollover import DynamicRolloverEngine

            target_spot = _safe_float(self.base_data.get("price"), 0.0)

            engine = DynamicRolloverEngine()
            six_rule_passed, six_rule_reason = await engine._confirm_entry_signal(
                self.symbol, self.base_data, target_spot
            )
            six_rule_reasons = six_rule_reason.split(" | ") if six_rule_reason else []

            embed = create_entry_rules_embed(
                self.symbol,
                six_rule_passed,
                six_rule_reasons,
            )
        except Exception as e:
            logger.exception(f"[{self.symbol}] Entry rules check failed: {e}")
            await interaction.followup.send(
                embed=create_error_embed(f"進場鐵律檢核失敗: {e}"), ephemeral=True
            )
        finally:
            await self._reset_loading(interaction, embed=embed)


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
