"""基本面分析管線互動診斷終端 Cog (/fa 指令)。

架構原則：
1. 輸出集中化：嚴格透過 cogs.embed_builders.fundamental_embeds 構建 Embed，Cog 內禁止直接實例化 discord.Embed。
2. 唯讀互動：嚴格設置 ephemeral=True，保護用戶查詢隱私，完全無寫入副作用。
3. 開閉原則：採用 FaSectionRegistry 註冊機制，支援各 PR 漸進式掛載診斷區塊。
4. 零執行不變量：顧問性評級展示，絕無自動下單或平倉。
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Protocol
import discord
from discord import app_commands
from discord.ext import commands

from cogs.embed_builders.fundamental_embeds import build_fa_terminal_embed
from database.fundamental_pipeline import (
    get_active_governance_flags,
    get_insider_transactions,
    get_latest_liquidity_regime,
    get_sec_filing_cursor,
)
from market_analysis.fundamental_pipeline.governance_gate import (
    evaluate_governance_status,
)
from market_analysis.fundamental_pipeline.insider_signal import (
    evaluate_insider_signal,
)

logger = logging.getLogger(__name__)


class FaSection(Protocol):
    """基本面診斷終端區塊介面契約。"""

    @property
    def section_id(self) -> str:
        """區塊唯一識別代號。"""
        ...

    async def render(self, symbol: str) -> tuple[str, str] | None:
        """渲染並產出 (欄位標題, 欄位內文)；若無資料可回傳 None。"""
        ...


class FaSectionRegistry:
    """基本面終端區塊註冊中心。"""

    def __init__(self) -> None:
        self._sections: list[FaSection] = []

    def register(self, section: FaSection) -> None:
        """註冊新的診斷區塊。"""
        self._sections.append(section)

    async def render_all(self, symbol: str) -> list[tuple[str, str]]:
        """按註冊順序渲染所有已掛載的區塊。"""
        results: list[tuple[str, str]] = []
        for sec in self._sections:
            try:
                rendered = await sec.render(symbol)
                if rendered is not None:
                    results.append(rendered)
            except Exception as e:
                logger.error(
                    f"[FaSectionRegistry] 渲染區塊 {sec.section_id} 失敗 ({symbol}): {e}"
                )
                results.append((f"⚠️ {sec.section_id}", "資料載入異常"))
        return results


# ============================================================================
# 預設掛載區塊實作 (PR1 & PR2)
# ============================================================================


class MacroLiquiditySection:
    """PR1: 宏觀流動性體制區塊。"""

    @property
    def section_id(self) -> str:
        return "macro_liquidity"

    async def render(self, symbol: str) -> tuple[str, str]:
        reading = await asyncio.to_thread(get_latest_liquidity_regime)
        header = "🌊 宏觀流動性體制 (Macro Liquidity)"
        if reading is None:
            return header, "• 當前體制: UNKNOWN ｜ 暫無最新流動性讀數"

        nfci_str = f"{reading.nfci:.2f}" if reading.nfci is not None else "--"
        chg_str = (
            f"{reading.net_liquidity_chg_13w_pct:+.1f}%"
            if reading.net_liquidity_chg_13w_pct is not None
            else "--%"
        )
        erp_str = (
            f"{reading.equity_risk_premium:.2%}"
            if reading.equity_risk_premium is not None
            else "--%"
        )
        us10y_str = f"{reading.us10y:.2f}%" if reading.us10y is not None else "--%"

        lines = [
            f"• 當前體制: **{reading.regime}** (NFCI: `{nfci_str}` ｜ 淨流動性季增: `{chg_str}`)",
            f"• 隱含股權風險溢價 (ERP): `{erp_str}` (無風險 10Y: `{us10y_str}`)",
        ]
        return header, "\n".join(lines)


class GovernanceGateSection:
    """PR2: 治理審查閘門與高管內部人交易區塊。"""

    @property
    def section_id(self) -> str:
        return "governance_gate"

    async def render(self, symbol: str) -> tuple[str, str]:
        sym_upper = symbol.strip().upper()
        header = "🚨 治理與重大事件監控 (Governance Gate)"

        # SEC 申報同步管線尚未排程：從未同步（無游標）時不得把「沒有資料」顯示成
        # 「正常」或 NEUTRAL，必須與「已同步且乾淨」明確區分。
        cursor = await asyncio.to_thread(get_sec_filing_cursor, sym_upper)
        if cursor is None:
            no_data = "⚪ 尚無申報同步資料（SEC 申報同步管線尚未排程）"
            return (
                header,
                f"• 治理狀態: {no_data}\n• 內部人行為 (30D): {no_data}",
            )

        # 讀取生效中之治理旗標
        flags = await asyncio.to_thread(get_active_governance_flags, sym_upper)
        gov_status = evaluate_governance_status(sym_upper, flags)

        # 讀取最近 30 天內部人交易
        txs = await asyncio.to_thread(get_insider_transactions, sym_upper, 30)
        insider_summary = evaluate_insider_signal(sym_upper, txs, window_days=30)

        synced_at = cursor.updated_at[:16] if cursor.updated_at else "--"
        if gov_status.is_clean:
            gov_line = (
                "• 治理狀態: 🟢 已同步，最近未觸發 4.02 / 5.02 警訊"
                f"（最後同步 {synced_at} UTC）"
            )
        else:
            sev_icon = {"CRITICAL": "🔴", "HIGH": "🟠", "REVIEW": "🟡"}.get(
                gov_status.max_severity or "", "⚪"
            )
            if gov_status.max_severity in ("CRITICAL", "HIGH"):
                status_text = "**觸發風控審查**"
            else:
                status_text = "**待人工複核**（5.02 僅有 item code，未判定為離任）"
            gov_line = (
                f"• 治理狀態: {sev_icon} {status_text} "
                f"({gov_status.max_severity}，共 {len(gov_status.active_flags)} 項旗標生效中)"
            )

        insider_icon = (
            "🟢"
            if insider_summary.verdict == "CLUSTER_BUY"
            else ("🔴" if insider_summary.verdict == "HEAVY_INSIDER_SALE" else "⚪")
        )
        insider_line = (
            f"• 內部人行為 (30D): {insider_icon} **{insider_summary.verdict}** "
            f"({insider_summary.summary_text})"
        )

        return header, f"{gov_line}\n{insider_line}"


# 全域單例區塊註冊中心
fa_section_registry = FaSectionRegistry()
fa_section_registry.register(MacroLiquiditySection())
fa_section_registry.register(GovernanceGateSection())


# ============================================================================
# Discord View & Cog
# ============================================================================


class FaTerminalView(discord.ui.View):
    """基本面診斷終端輔助按鈕列。"""

    def __init__(self, symbol: str) -> None:
        super().__init__(timeout=180.0)
        sec_url = f"https://www.sec.gov/edgar/browse/?CIK={symbol}"
        self.add_item(
            discord.ui.Button(
                label="🔍 前往 SEC EDGAR 查看原始申報",
                url=sec_url,
                style=discord.ButtonStyle.link,
            )
        )


class FundamentalTerminalCog(commands.Cog, name="FundamentalTerminalCog"):
    """基本面分析管線互動診斷終端指令 Cog。"""

    def __init__(self, bot: Any) -> None:
        self.bot = bot

    @app_commands.command(
        name="fa",
        description="查詢標的資產之基本面事件、治理閘門與全景估值雷達",
    )
    @app_commands.describe(
        symbol="欲查詢之美股股票代碼（例如：TSLA, NVDA, AAPL）",
    )
    async def fa_command(
        self,
        interaction: discord.Interaction,
        symbol: str,
    ) -> None:
        """執行 /fa 互動診斷查詢。"""
        # 遵循個人隱私保護：強制使用 ephemeral=True
        await interaction.response.defer(ephemeral=True)

        sym_clean = symbol.strip().upper()
        if not sym_clean or len(sym_clean) > 10:
            await interaction.followup.send(
                "❌ 請輸入正確的美股股票代碼格式（如 TSLA, NVDA）。",
                ephemeral=True,
            )
            return

        # 解析公司名稱
        company_name = sym_clean
        try:
            from services.market_data_service.fundamentals import get_company_profile

            profile = await get_company_profile(sym_clean)
            if profile and profile.get("name"):
                company_name = str(profile["name"])
        except Exception:
            pass

        # 遍歷註冊中心渲染所有區塊
        sections = await fa_section_registry.render_all(sym_clean)

        # 構建專屬 NexusEmbed 與輔助 View
        embed = build_fa_terminal_embed(
            symbol=sym_clean,
            company_name=company_name,
            sections=sections,
        )
        view = FaTerminalView(sym_clean)

        await interaction.followup.send(embed=embed, view=view, ephemeral=True)


async def setup(bot: Any) -> None:
    """載入 FundamentalTerminalCog。"""
    await bot.add_cog(FundamentalTerminalCog(bot))
