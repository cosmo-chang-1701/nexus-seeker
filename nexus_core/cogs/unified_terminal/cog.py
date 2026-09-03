from typing import Any
import discord
from discord.ext import commands
from discord import app_commands
import asyncio
import logging
from typing import Optional

from services import market_data_service
import database

from cogs.embed_builder import (
    create_error_embed,
    create_strategic_dash_embed,
    build_market_macro_overview_embed,
)

from .utils import get_macro_overview_data
from .portfolio_view import PortfolioHubView
from .pulse_view import PulseHubView
from .batch_scan import BatchScanMixin
from .symbol_deep_dive import SymbolDeepDiveMixin
from .radar_data import RadarDataMixin

logger = logging.getLogger(__name__)


class UnifiedTerminalCog(
    BatchScanMixin, SymbolDeepDiveMixin, RadarDataMixin, commands.Cog
):
    """
    Unified Hubs for Nexus Seeker.
    Consolidates 20+ commands into 3 core hubs: /x, /dash, /market.
    """

    def __init__(self, bot: Any):
        self.bot = bot
        logger.info("UnifiedTerminalCog loaded.")

    @app_commands.command(
        name="x", description="🌌 標的分析中心：一站式獲取報價、量化掃描與情緒分析"
    )
    @app_commands.describe(
        symbol="股票代號 (如 NVDA，與 scan_type 二擇一)",
        scan_type="批次掃描類型 (留空則開啟量化雷達面板)",
        tag="Watchlist 標籤過濾 (僅在 scan_type 為 WATCHLIST 時生效)",
        squeeze="僅顯示正處於擠壓狀態的標的",
    )
    @app_commands.choices(
        scan_type=[
            app_commands.Choice(name="💼 掃描持倉標的 (Holdings)", value="HOLDINGS"),
            app_commands.Choice(
                name="⏳ 掃描掛單標的 (Pending Orders)", value="ORDERS"
            ),
            app_commands.Choice(
                name="📜 掃描期權持倉標的 (Option Holdings)", value="OPTIONS"
            ),
            app_commands.Choice(name="🌟 掃描自選標的 (Watchlist)", value="WATCHLIST"),
            app_commands.Choice(name="🌀 掃描全部 (持倉+掛單+期權標的)", value="ALL"),
        ]
    )
    async def symbol_hub(
        self,
        interaction: discord.Interaction,
        symbol: Optional[str] = None,
        scan_type: Optional[app_commands.Choice[str]] = None,
        tag: Optional[str] = None,
        squeeze: Optional[bool] = None,
    ) -> Any:
        await interaction.response.defer(ephemeral=True)
        try:
            user_id = interaction.user.id

            # 🚀 Task 2 Hook: Proactive Warmup during pre-market window (08:30 - 09:30 ET)
            if hasattr(self.bot, "memory_manager"):
                coro = self.bot.memory_manager.proactive_warmup()
                if asyncio.iscoroutine(coro):
                    asyncio.create_task(coro)

            # 1. 參數驗證
            # (移除了 symbol 與 scan_type 的強制驗證，因為現在沒有帶參數會開啟控制面板)

            # 2. 單一標的深度分析
            if symbol:
                symbol = symbol.upper()
                await self._run_single_symbol_hub(interaction, symbol, user_id)
                return

            # 3. 批次掃描邏輯 / 開啟面板
            if not scan_type:
                from .radar_view import UnifiedRadarView
                from cogs.embed_builders.scan_embeds import (
                    build_unified_radar_panel_embed,
                )

                view = UnifiedRadarView(self, user_id)
                embed = build_unified_radar_panel_embed(view.get_state_dict())
                return await interaction.followup.send(
                    embed=embed, view=view, ephemeral=True
                )

            scan_value = scan_type.value

            # 建立相容舊參數的 State Dict 供引擎使用
            state = {
                "scope": scan_value,
                "quant_filters": ["squeeze_mode"] if squeeze else [],
                "params": {
                    "max_pain_threshold": 10.0,
                    "abs_support_tolerance": 1.0,
                    "silent_period_days": 5,
                },
                "selected_tag": tag,
            }

            await self.execute_unified_scan(interaction, state, user_id)

        except Exception as outer_err:
            logger.error(f"Outer Symbol Hub Error: {outer_err}")
            try:
                await interaction.followup.send(
                    embed=create_error_embed(
                        f"執行 `/x` 指令時發生未預期錯誤: {outer_err}"
                    ),
                    ephemeral=True,
                )
            except Exception as follow_err:
                logger.error(f"Failed to send outer error followup: {follow_err}")

    @symbol_hub.autocomplete("tag")
    async def tag_autocomplete(
        self,
        interaction: discord.Interaction,
        current: str,
    ) -> list[app_commands.Choice[str]]:
        from database.watchlist_tags import get_user_unique_tags
        import asyncio

        user_id_str = str(interaction.user.id)

        try:
            tags = await asyncio.to_thread(get_user_unique_tags, user_id_str)
        except Exception:
            tags = []

        return [
            app_commands.Choice(name=t, value=t)
            for t in tags
            if current.lower() in t.lower()
        ][:25]

    @app_commands.command(
        name="dash", description="📊 交易員看板：一站式監控持倉、跑道與 VTR 績效"
    )
    async def portfolio_hub(self, interaction: discord.Interaction) -> Any:
        await interaction.response.defer(ephemeral=True)
        user_id = interaction.user.id

        # 🚀 Task 2 Hook: Proactive Warmup during pre-market window
        if hasattr(self.bot, "memory_manager"):
            coro = self.bot.memory_manager.proactive_warmup()
            if asyncio.iscoroutine(coro):
                asyncio.create_task(coro)

        from services.trading_service import TradingService
        from services.asset_manager import AssetManager
        from models.asset import ContextType, HoldingMetadata
        from market_analysis.pro_management import calculate_financial_runway

        trading_service = TradingService(self.bot)
        pnl_data = await trading_service.get_portfolio_pnl(user_id)
        ctx = database.get_full_user_context(user_id)

        manager = AssetManager()
        holdings = manager.get_assets(user_id, ContextType.HOLDING)
        total_holding_value = 0.0
        with market_data_service.mark_interactive_request():
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

            # 獲取 VIX 資訊
            macro_raw = await market_data_service.get_macro_environment()
            vix_spot = macro_raw.get("vix", 18.0)

        embed = create_strategic_dash_embed(
            ctx,
            pnl_data,
            vix_spot=vix_spot,
            backup_liquidity=backup_liq,
            extended_runway=ext_runway,
        )

        view = PortfolioHubView(user_id, self.bot)
        await interaction.followup.send(embed=embed, view=view, ephemeral=True)

    @app_commands.command(
        name="market", description="🌌 市場情報中心：監控日曆、預測市場與高波動標的"
    )
    async def pulse_hub(self, interaction: discord.Interaction) -> Any:
        await interaction.response.defer(ephemeral=True)

        # 🚀 Task 2 Hook: Proactive Warmup during pre-market window
        if hasattr(self.bot, "memory_manager"):
            coro = self.bot.memory_manager.proactive_warmup()
            if asyncio.iscoroutine(coro):
                asyncio.create_task(coro)

        with market_data_service.mark_interactive_request():
            macro_data = await get_macro_overview_data(interaction.user.id)
        embed = build_market_macro_overview_embed(macro_data)

        view = PulseHubView(interaction.user.id, self.bot)
        await interaction.followup.send(embed=embed, view=view, ephemeral=True)

    @app_commands.command(
        name="stress_test",
        description="🚨 GTC 掛單現金赤字壓力測試 (Worst-Case Stress Test)",
    )
    async def stress_test(self, interaction: discord.Interaction) -> Any:
        await interaction.response.defer(ephemeral=True)
        user_id = interaction.user.id

        try:
            from database.orders import get_user_active_orders

            orders = get_user_active_orders(user_id)
            total_deficit = 0.0
            gtc_buy_orders = []
            for o in orders:
                validity = o.get("validity", "").upper()
                side = o.get("side", "").upper()
                if "GTC" in validity and side == "BUY":
                    price = o.get("limit_price", 0.0)
                    if price <= 0.0:
                        price = o.get("stop_price", 0.0)
                    qty = o.get("quantity", 0.0)
                    total_deficit += price * qty
                    gtc_buy_orders.append(o)
            ctx = database.get_full_user_context(user_id)
            cash_reserve = ctx.cash_reserve if ctx else 0.0

            from database.holdings import get_user_holdings

            holdings = get_user_holdings(user_id)
            boxx_shares = 0.0
            for h in holdings:
                if h.get("symbol", "").upper() == "BOXX":
                    boxx_shares = h.get("quantity", 0.0)
                    break
            boxx_cash = min(boxx_shares, 180.0) * (21000.0 / 180.0)
            net_deficit = cash_reserve + boxx_cash - total_deficit
            is_critical = total_deficit > (cash_reserve + boxx_cash)

            results = {
                "total_deficit": total_deficit,
                "cash_reserve": cash_reserve,
                "boxx_shares": boxx_shares,
                "boxx_cash": boxx_cash,
                "net_deficit": net_deficit,
                "is_critical": is_critical,
                "gtc_buy_orders_count": len(gtc_buy_orders),
            }
            from cogs.embed_builder import create_stress_test_embed

            embed = create_stress_test_embed(results)
            await interaction.followup.send(embed=embed, ephemeral=True)
        except Exception as e:
            await interaction.followup.send(
                embed=create_error_embed(f"壓力測試失敗: {e}"), ephemeral=True
            )
