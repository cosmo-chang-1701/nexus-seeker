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
        squeeze="僅顯示擠壓觸發且動能轉正 (Squeeze Firing) 的標的",
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

            # 1. 參數驗證：兩者皆未帶會開啟控制面板；兩者同時帶則違反「二擇一」
            # 契約，明確拒絕而非靜默丟棄 scan_type（使用者會以為批次掃描有執行）。
            if symbol is not None and scan_type is not None:
                return await interaction.followup.send(
                    embed=create_error_embed(
                        "`symbol` 與 `scan_type` 只能擇一填寫：分析單一標的請只填 "
                        "`symbol`，批次掃描請只填 `scan_type`。",
                        title="輸入錯誤",
                    ),
                    ephemeral=True,
                )

            # 2. 單一標的深度分析（代號正規化與格式驗證由 _run_single_symbol_hub
            # 統一處理：去空白、去 `$` 前後綴、轉大寫）
            if symbol is not None:
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

            # tag 僅對 WATCHLIST 掃描有意義；其他範圍明確清空。資料庫內的標籤一律
            # 以 sanitize_tags() 大寫化儲存，而 slash 參數允許自由輸入（不強制走
            # autocomplete），這裡同步去空白、轉大寫以免比對落空。
            normalized_tag: Optional[str] = None
            if scan_value == "WATCHLIST" and tag is not None:
                normalized_tag = tag.strip().upper() or None

            # 建立相容舊參數的 State Dict 供引擎使用
            state = {
                "scope": scan_value,
                "quant_filters": ["squeeze_mode"] if squeeze else [],
                "params": {
                    "max_pain_threshold": 10.0,
                    "abs_support_tolerance": 1.0,
                    "silent_period_days": 5,
                },
                "selected_tag": normalized_tag,
            }

            await self.execute_unified_scan(interaction, state, user_id)

        except Exception as outer_err:
            # 例外細節（可能含內部路徑、SQL、第三方 API 回應）只寫入日誌，
            # 不直接顯示給使用者。
            logger.exception(f"Outer Symbol Hub Error: {outer_err}")
            try:
                await interaction.followup.send(
                    embed=create_error_embed(
                        "執行 `/x` 指令時發生未預期錯誤，請稍後再試。"
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
        from services.withdrawal_runway_service import get_runway_display

        trading_service = TradingService(self.bot)
        pnl_data = await trading_service.get_portfolio_pnl(user_id)
        ctx = database.get_full_user_context(user_id)
        runway, runway_stale = await get_runway_display(user_id)

        with market_data_service.mark_interactive_request():
            # 獲取 VIX 資訊（未知時為 None，看板標示「資料不足」而非冒用 18.0）
            vix_spot = await market_data_service.get_vix_spot_strict()

        nav_data = await trading_service.get_market_nav(
            user_id, float(ctx.cash_reserve or 0.0), pnl_data
        )
        spy_quote = await market_data_service.get_quote("SPY")
        spy_raw = spy_quote.get("c") if spy_quote else None
        spy_now = float(spy_raw) if spy_raw and float(spy_raw) > 0 else None

        embed = create_strategic_dash_embed(
            ctx,
            pnl_data,
            vix_spot=vix_spot,
            runway=runway,
            runway_stale=runway_stale,
            nav_data=nav_data,
            spy_price=spy_now,
        )

        view = PortfolioHubView(user_id, self.bot)
        await interaction.followup.send(embed=embed, view=view, ephemeral=True)

    @app_commands.command(
        name="market", description="🌌 市場情報中心：監控日曆、預測市場與高波動標的"
    )
    async def pulse_hub(self, interaction: discord.Interaction) -> Any:
        await interaction.response.defer(ephemeral=True)

        try:
            # 🚀 Task 2 Hook: Proactive Warmup during pre-market window
            if hasattr(self.bot, "memory_manager"):
                coro = self.bot.memory_manager.proactive_warmup()
                if asyncio.iscoroutine(coro):
                    asyncio.create_task(coro)

            with market_data_service.mark_interactive_request():
                macro_data = await get_macro_overview_data(interaction.user.id)
            embed = build_market_macro_overview_embed(macro_data)

            view = PulseHubView(interaction.user.id, self.bot)
            # 首則 followup 會取代 defer 的「思考中」原始回應，故 slash 指令本身的
            # interaction 即可在 view 逾時時編輯此訊息
            view.last_interaction = interaction
            await interaction.followup.send(embed=embed, view=view, ephemeral=True)
        except Exception:
            # defer 之後若未送出任何 followup，使用者會永遠停在「思考中」
            logger.exception("/market 市場情報中心載入失敗")
            try:
                await interaction.followup.send(
                    embed=create_error_embed(
                        "載入市場情報中心時發生未預期錯誤，請稍後再試。"
                    ),
                    ephemeral=True,
                )
            except Exception as follow_err:
                logger.error(f"/market 錯誤訊息回覆失敗: {follow_err}")

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
