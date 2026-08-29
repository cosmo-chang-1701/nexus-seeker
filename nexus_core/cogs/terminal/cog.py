from typing import Any, Optional
import logging

import discord
from discord.ext import commands
from discord import app_commands

from services import market_data_service

from . import (
    analysis,
    holdings,
    price_alerts,
    rollover,
    settings,
    system,
    trades,
    watchlist,
)

logger = logging.getLogger(__name__)


class TerminalCog(commands.Cog):
    """
    [Core] Nexus Seeker Professional Terminal Interface.
    Retains only the high-impact commands for professional operations.
    """

    def __init__(self, bot: Any):
        self.bot = bot
        logger.info("TerminalCog loaded.")

        # 為了相容集成測試與 Embed 驗證，我們動態將 update_settings.callback 包裝成支援關鍵字參數的形式
        # 這樣一來，斜線指令在 Discord 註冊時仍然是完全不帶參數的（使用者點選即可喚起面板），但 Python 測試可以直接傳參
        async def compat_callback(cog: Any, interaction: Any, **kwargs):  # type: ignore
            return await cog._update_settings_impl(interaction, **kwargs)

        self.update_settings._callback = compat_callback

    # ------------------------------------------------------------------
    # 帳戶設定 / 通知偏好 / WTI 警報
    # ------------------------------------------------------------------

    @app_commands.command(
        name="settings", description="配置帳戶全域參數 (資金、風險與專業營運指標)"
    )
    async def update_settings(self, interaction: discord.Interaction) -> Any:
        """喚起帳戶設定互動式面板"""
        await self._update_settings_impl(interaction)

    async def _update_settings_impl(
        self,
        interaction: discord.Interaction,
        capital: Optional[float] = None,
        risk_limit: Optional[float] = None,
        enable_vtr: Optional[bool] = None,
        enable_psq_watchlist: Optional[bool] = None,
        polymarket_threshold: Optional[float] = None,
        polymarket_use_llm: Optional[bool] = None,
        polymarket_slippage: Optional[float] = None,
        monthly_expense: Optional[float] = None,
        tax_reserve_rate: Optional[float] = None,
        cash_reserve: Optional[float] = None,
    ) -> Any:
        """喚起帳戶設定互動式面板，或直接配置特定參數"""
        return await settings.update_settings_impl(
            interaction,
            capital=capital,
            risk_limit=risk_limit,
            enable_vtr=enable_vtr,
            enable_psq_watchlist=enable_psq_watchlist,
            polymarket_threshold=polymarket_threshold,
            polymarket_use_llm=polymarket_use_llm,
            polymarket_slippage=polymarket_slippage,
            monthly_expense=monthly_expense,
            tax_reserve_rate=tax_reserve_rate,
            cash_reserve=cash_reserve,
        )

    @app_commands.command(
        name="notif_settings",
        description="自訂通知偏好設定中心 (開啟或關閉背景定時報告與即時風控警報)",
    )
    async def notif_settings(self, interaction: discord.Interaction) -> Any:
        """喚起自訂通知設定面板"""
        return await settings.notif_settings_impl(interaction)

    @app_commands.command(
        name="wti_config",
        description="🛢️ 設定 WTI 原油價格警報閾值 (上限/下限/30分波動%)",
    )
    async def wti_config(self, interaction: discord.Interaction) -> Any:
        """喚起 WTI 原油警報閾值設定彈窗"""
        return await settings.wti_config_impl(interaction)

    # ------------------------------------------------------------------
    # TRADE（實單期權）
    # ------------------------------------------------------------------

    @app_commands.command(name="add_trade", description="將新的選擇權部位加入監控管線")
    @app_commands.choices(
        opt_type=[
            app_commands.Choice(name="Put (賣權)", value="put"),
            app_commands.Choice(name="Call (買權)", value="call"),
        ]
    )
    @app_commands.describe(
        symbol="股票代號 (如 TSLA)",
        opt_type="策略類型",
        strike="履約價",
        expiry="到期日 (YYYY-MM-DD)",
        entry_price="成交價格",
        quantity="口數",
    )
    async def add_trade(
        self,
        interaction: discord.Interaction,
        symbol: str,
        opt_type: app_commands.Choice[str],
        strike: float,
        expiry: str,
        entry_price: float,
        quantity: int,
    ) -> Any:
        return await trades.add_trade_impl(
            interaction, symbol, opt_type, strike, expiry, entry_price, quantity
        )

    @app_commands.command(
        name="edit_trade", description="修改實單交易參數 (履約價、到期日、價格或口數)"
    )
    @app_commands.describe(
        trade_id="資產 ID (從 /list_trades 獲取)",
        strike="更新履約價 (選填)",
        expiry="更新到期日 YYYY-MM-DD (選填)",
        price="更新成交價格 (選填)",
        quantity="更新口數 (選填)",
        category="更新類別 SPECULATIVE/HEDGE (選填)",
    )
    @app_commands.choices(
        category=[
            app_commands.Choice(name="SPECULATIVE", value="SPECULATIVE"),
            app_commands.Choice(name="HEDGE", value="HEDGE"),
        ]
    )
    async def edit_trade(
        self,
        interaction: discord.Interaction,
        trade_id: int,
        strike: Optional[float] = None,
        expiry: Optional[str] = None,
        price: Optional[float] = None,
        quantity: Optional[int] = None,
        category: Optional[app_commands.Choice[str]] = None,
    ) -> Any:
        return await trades.edit_trade_impl(
            interaction, trade_id, strike, expiry, price, quantity, category
        )

    @app_commands.command(
        name="list_trades", description="列出目前資料庫中的所有實單持倉與未實現損益"
    )
    @market_data_service.interactive
    async def list_trades(self, interaction: discord.Interaction) -> Any:
        return await trades.list_trades_impl(self.bot, interaction)

    @app_commands.command(name="remove_trade", description="將部位從監控管線中移除")
    async def remove_trade(
        self, interaction: discord.Interaction, trade_id: int
    ) -> Any:
        return await trades.remove_trade_impl(interaction, trade_id)

    @app_commands.command(
        name="settle_trade", description="將實單交易結算為現貨持倉 (TRADE -> HOLDING)"
    )
    @app_commands.describe(
        asset_id="資產 ID (從 /list_trades 獲取)",
        execution_price="最終執行價格 (用於計算平均成本)",
    )
    async def settle_trade(
        self, interaction: discord.Interaction, asset_id: int, execution_price: float
    ) -> Any:
        return await trades.settle_trade_impl(interaction, asset_id, execution_price)

    # ------------------------------------------------------------------
    # WATCH（觀察清單）
    # ------------------------------------------------------------------

    @app_commands.command(
        name="add_watch",
        description="將標的加入自動化量化監控清單 (WATCH)，可用逗號或空白分隔一次加入多檔",
    )
    @app_commands.describe(
        symbol="股票代號 (如 TSLA，可一次輸入多檔，用逗號或空白分隔，如 'AAPL, TSLA NVDA')"
    )
    async def add_watch(self, interaction: discord.Interaction, symbol: str) -> Any:
        return await watchlist.add_watch_impl(interaction, symbol)

    @app_commands.command(
        name="remove_watch",
        description="將標的從觀察清單中移除，可用逗號或空白分隔一次移除多檔",
    )
    @app_commands.describe(
        symbol="股票代號 (可一次輸入多檔，用逗號或空白分隔，如 'AAPL, TSLA NVDA')"
    )
    async def remove_watch(self, interaction: discord.Interaction, symbol: str) -> Any:
        return await watchlist.remove_watch_impl(interaction, symbol)

    @app_commands.command(name="list_watch", description="列出您的雷達觀察清單")
    @app_commands.choices(
        sort=[
            app_commands.Choice(name="加入時間 (預設)", value="created"),
            app_commands.Choice(name="字母 A→Z", value="alpha"),
            app_commands.Choice(name="標籤", value="tags"),
        ]
    )
    @app_commands.describe(
        sort="排序方式 (預設依加入時間)",
        query="依標的代號或標籤搜尋 (不分大小寫，選填)",
    )
    async def list_watch(
        self,
        interaction: discord.Interaction,
        sort: Optional[app_commands.Choice[str]] = None,
        query: Optional[str] = None,
    ) -> Any:
        return await watchlist.list_watch_impl(interaction, sort, query)

    @app_commands.command(
        name="promote_watch", description="將觀察標的提升為實單交易 (WATCH -> TRADE)"
    )
    @app_commands.describe(
        symbol="股票代號",
        opt_type="期權類型 (call/put)",
        strike="履約價",
        expiry="到期日 (YYYY-MM-DD)",
        price="成交價格",
        qty="口數",
    )
    async def promote_watch(
        self,
        interaction: discord.Interaction,
        symbol: str,
        opt_type: str,
        strike: float,
        expiry: str,
        price: float,
        qty: int,
    ) -> Any:
        return await watchlist.promote_watch_impl(
            interaction, symbol, opt_type, strike, expiry, price, qty
        )

    # ------------------------------------------------------------------
    # HOLDING（現貨持倉）
    # ------------------------------------------------------------------

    @app_commands.command(name="add_holding", description="登錄實際現貨持倉 (HOLDING)")
    @app_commands.describe(
        symbol="股票代號",
        quantity="持有股數",
        avg_cost="平均買入成本 (USD)",
        asset_class="核心 (CORE) 或衛星 (SATELLITE) 資產分類，供動態轉倉引擎再平衡判斷 (選填)",
        max_allocation_pct="資產配置佔總市值上限的百分比 (0-100，例如 30 代表 30%，選填)",
        target_allocation_pct="超限時再平衡的目標配置百分比 (0-100，需小於等於配置上限，選填)",
        boxx_allocation_pct="核心資金部署觸發時，優先轉入 BOXX 防禦的判定閾值 (0-100，≥50 優先防禦轉入 BOXX；留空則由系統依當前總經數據自動評估建議值，選填)",
        acquired_at="建倉日期 (YYYY-MM-DD)，供動態轉倉引擎估算長/短期資本利得稅率區間；留空則預設為今天 (選填)",
    )
    @app_commands.choices(
        asset_class=[
            app_commands.Choice(name="CORE (核心防禦資產，如 VOO/BOXX)", value="CORE"),
            app_commands.Choice(name="SATELLITE (衛星戰術資產)", value="SATELLITE"),
        ]
    )
    async def add_holding(
        self,
        interaction: discord.Interaction,
        symbol: str,
        quantity: float,
        avg_cost: float,
        asset_class: Optional[app_commands.Choice[str]] = None,
        max_allocation_pct: Optional[float] = None,
        target_allocation_pct: Optional[float] = None,
        boxx_allocation_pct: Optional[float] = None,
        acquired_at: Optional[str] = None,
    ) -> Any:
        return await holdings.add_holding_impl(
            interaction,
            symbol,
            quantity,
            avg_cost,
            asset_class,
            max_allocation_pct,
            target_allocation_pct,
            boxx_allocation_pct,
            acquired_at,
        )

    @app_commands.command(
        name="edit_holding",
        description="修改現貨持倉參數 (數量、成本、核心/衛星分類或配置上限)",
    )
    @app_commands.describe(
        symbol="股票代號",
        quantity="更新後的持有股數 (選填)",
        avg_cost="更新後的平均成本 (選填)",
        asset_class="核心 (CORE) 或衛星 (SATELLITE) 資產分類，供動態轉倉引擎再平衡判斷 (選填)",
        max_allocation_pct="資產配置佔總市值上限的百分比 (0-100，例如 30 代表 30%，選填)",
        target_allocation_pct="超限時再平衡的目標配置百分比 (0-100，需小於等於配置上限，選填)",
        boxx_allocation_pct="核心資金部署觸發時，優先轉入 BOXX 防禦的判定閾值 (0-100，≥50 優先防禦轉入 BOXX；留空則由系統依當前總經數據自動評估建議值，選填)",
        acquired_at="建倉日期 (YYYY-MM-DD)，用於回填校正實際開倉日以利長/短期資本利得稅務提醒 (選填)",
    )
    @app_commands.choices(
        asset_class=[
            app_commands.Choice(name="CORE (核心防禦資產，如 VOO/BOXX)", value="CORE"),
            app_commands.Choice(name="SATELLITE (衛星戰術資產)", value="SATELLITE"),
        ]
    )
    async def edit_holding(
        self,
        interaction: discord.Interaction,
        symbol: str,
        quantity: Optional[float] = None,
        avg_cost: Optional[float] = None,
        asset_class: Optional[app_commands.Choice[str]] = None,
        max_allocation_pct: Optional[float] = None,
        target_allocation_pct: Optional[float] = None,
        boxx_allocation_pct: Optional[float] = None,
        acquired_at: Optional[str] = None,
    ) -> Any:
        return await holdings.edit_holding_impl(
            interaction,
            symbol,
            quantity,
            avg_cost,
            asset_class,
            max_allocation_pct,
            target_allocation_pct,
            boxx_allocation_pct,
            acquired_at,
        )

    @app_commands.command(
        name="list_holdings", description="列出目前所有現貨持倉、分配比例與即時損益估計"
    )
    @market_data_service.interactive
    async def list_holdings(self, interaction: discord.Interaction) -> Any:
        return await holdings.list_holdings_impl(interaction)

    @app_commands.command(
        name="remove_holding", description="從資產清單中移除特定的現貨紀錄"
    )
    @app_commands.describe(symbol="要移除的股票代號")
    async def remove_holding(
        self, interaction: discord.Interaction, symbol: str
    ) -> Any:
        return await holdings.remove_holding_impl(interaction, symbol)

    # ------------------------------------------------------------------
    # 量化掃描 / 部位轉換模擬 / VTR
    # ------------------------------------------------------------------

    @app_commands.command(
        name="scan", description="手動執行量化掃描與 What-if 曝險模擬"
    )
    async def manual_scan(self, interaction: discord.Interaction, symbol: str) -> Any:
        return await analysis.manual_scan_impl(interaction, symbol)

    @app_commands.command(
        name="transition_sim",
        description="模擬投機部位向 Core Equity/Covered Call 演進",
    )
    @app_commands.describe(
        symbol="標的代號",
        current_option_pnl="目前該部位累計未實現損益 (USD)",
        target_cc_strike="預計轉換後的 Covered Call 履約價",
        target_cc_premium="預計單次收租權利金 (USD)",
    )
    async def transition_sim(
        self,
        interaction: discord.Interaction,
        symbol: str,
        current_option_pnl: float,
        target_cc_strike: float,
        target_cc_premium: float,
    ) -> Any:
        return await analysis.transition_sim_impl(
            interaction,
            symbol,
            current_option_pnl,
            target_cc_strike,
            target_cc_premium,
        )

    @app_commands.command(
        name="vtr_stats", description="檢視虛擬交易室的績效統計與對沖歸因"
    )
    async def vtr_stats(self, interaction: discord.Interaction) -> Any:
        return await analysis.vtr_stats_impl(interaction)

    @app_commands.command(
        name="vtr_list", description="列出虛擬交易室中的所有持倉與歷史紀錄"
    )
    async def vtr_list(self, interaction: discord.Interaction) -> Any:
        return await analysis.vtr_list_impl(interaction)

    # ------------------------------------------------------------------
    # 系統診斷
    # ------------------------------------------------------------------

    @app_commands.command(
        name="sys_health", description="[Hidden] 檢查系統資源狀態與記憶體健康度"
    )
    async def sys_health(self, interaction: discord.Interaction) -> Any:
        return await system.sys_health_impl(self.bot, interaction)

    # ------------------------------------------------------------------
    # 動態轉倉引擎
    # ------------------------------------------------------------------

    async def _execute_verify_thesis_logic(
        self,
        interaction: discord.Interaction | None,
        symbol: str,
        combined_text: str,
        source_url: str,
        target_message: discord.Message | None = None,
        form_type: str = "",
        sections: dict[str, str] | None = None,
    ) -> None:
        return await rollover.execute_verify_thesis_logic(
            interaction,
            symbol,
            combined_text,
            source_url,
            target_message=target_message,
            form_type=form_type,
            sections=sections,
        )

    @app_commands.command(
        name="verify_thesis",
        description="[動態轉倉引擎] 手動驗證基本面假設是否破滅 (LLM)",
    )
    @app_commands.describe(
        symbol="欲驗證之標的代號 (例如 AMD)",
        news_context="最新的重大新聞或法說會關鍵摘要 (選填)",
    )
    async def verify_thesis(
        self,
        interaction: discord.Interaction,
        symbol: str,
        news_context: Optional[str] = None,
    ) -> Any:
        """手動觸發動態轉倉：情境 1 (原型假設破滅)"""
        return await rollover.verify_thesis_impl(
            self, interaction, symbol, news_context
        )

    @app_commands.command(
        name="rollover_history", description="查看動態轉倉引擎近期推送給您的建議紀錄"
    )
    async def rollover_history(self, interaction: discord.Interaction) -> Any:
        return await rollover.rollover_history_impl(interaction)

    # ------------------------------------------------------------------
    # 個股 15 分鐘價量突破警報
    # ------------------------------------------------------------------

    @app_commands.command(
        name="price_alert_set",
        description="📊 設定個股 15 分鐘價量突破警報 (目標價、方向、放量倍數)",
    )
    @app_commands.choices(
        direction=[
            app_commands.Choice(
                name="≥ 向上突破 (收盤價達到或超過目標價)", value="above"
            ),
            app_commands.Choice(
                name="≤ 向下跌破 (收盤價達到或低於目標價)", value="below"
            ),
        ]
    )
    @app_commands.describe(
        symbol="股票代號 (如 AAPL)",
        target_price="目標價",
        direction="觸發方向",
        volume_multiplier="放量倍數門檻 (相對 20 根 15 分鐘均量，預設 1.5，設為 0 則不限制成交量)",
    )
    async def price_alert_set(
        self,
        interaction: discord.Interaction,
        symbol: str,
        target_price: float,
        direction: app_commands.Choice[str],
        volume_multiplier: float = 1.5,
    ) -> Any:
        return await price_alerts.price_alert_set_impl(
            interaction, symbol, target_price, direction, volume_multiplier
        )

    @app_commands.command(
        name="price_alert_list",
        description="📊 列出目前所有個股 15 分鐘價量突破監測設定",
    )
    async def price_alert_list(self, interaction: discord.Interaction) -> Any:
        return await price_alerts.price_alert_list_impl(interaction)

    @app_commands.command(
        name="price_alert_remove",
        description="📊 移除一筆個股 15 分鐘價量突破監測",
    )
    @app_commands.describe(symbol="要移除的股票代號")
    async def price_alert_remove(
        self, interaction: discord.Interaction, symbol: str
    ) -> Any:
        return await price_alerts.price_alert_remove_impl(interaction, symbol)


async def setup(bot: Any):  # type: ignore
    await bot.add_cog(TerminalCog(bot))
