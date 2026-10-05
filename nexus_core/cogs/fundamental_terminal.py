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
from cogs.embed_builders.fundamental_embeds import build_fa_terminal_embed
from database.fundamental_pipeline import (
    get_active_governance_flags,
    get_channel_checks_by_symbol,
    get_eps_estimate_snapshots,
    get_insider_transactions,
    get_latest_earnings_surprise,
    get_latest_guidance_extraction,
    get_latest_liquidity_regime,
)
from discord import app_commands
from discord.ext import commands
from market_analysis.fundamental_pipeline.governance_gate import (
    evaluate_governance_status,
)
from market_analysis.fundamental_pipeline.guidance_delta import (
    compare_guidance,
)
from market_analysis.fundamental_pipeline.insider_signal import (
    evaluate_insider_signal,
)
from market_analysis.fundamental_pipeline.models import (
    ChannelCheckLogRecord,
    GuidanceExtraction,
)
from market_analysis.fundamental_pipeline.supply_chain_map import (
    get_links_for_symbol,
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

        # 讀取生效中之治理旗標
        flags = await asyncio.to_thread(get_active_governance_flags, sym_upper)
        gov_status = evaluate_governance_status(sym_upper, flags)

        # 讀取最近 30 天內部人交易
        txs = await asyncio.to_thread(get_insider_transactions, sym_upper, 30)
        insider_summary = evaluate_insider_signal(sym_upper, txs, window_days=30)

        header = "🚨 治理與重大事件監控 (Governance Gate)"

        if gov_status.is_clean:
            gov_line = "• 治理狀態: 🟢 正常無重大異常 (最近未觸發 4.02 / 5.02 警訊)"
        else:
            sev_icon = "🔴" if gov_status.max_severity == "CRITICAL" else "🟠"
            gov_line = (
                f"• 治理狀態: {sev_icon} **觸發風控審查** "
                f"({gov_status.max_severity}，共 {len(gov_status.active_flags)} 項警訊生效中)"
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


class EarningsSurpriseSection:
    """PR3: 財務預期差與管理層前瞻指引區塊。"""

    @property
    def section_id(self) -> str:
        return "earnings_surprise"

    async def render(self, symbol: str) -> tuple[str, str]:
        sym_upper = symbol.strip().upper()
        header = "📊 業績預期差與 PEAD 修正 (Surprise & PEAD)"

        # 讀取最新財報預期差
        latest_surprise = await asyncio.to_thread(
            get_latest_earnings_surprise, sym_upper
        )
        guidance = await asyncio.to_thread(get_latest_guidance_extraction, sym_upper)
        snapshots = await asyncio.to_thread(get_eps_estimate_snapshots, sym_upper)

        lines: list[str] = []

        if latest_surprise is None:
            lines.append(
                "• 業績預期差: 暫無近期季度財報發布記錄 (待 8-K Item 2.02 或共識快照)"
            )
        else:
            score_val = latest_surprise.composite_score
            score_icon = (
                "🟢"
                if score_val is not None and score_val > 0
                else ("🔴" if score_val is not None and score_val < 0 else "⚪")
            )
            score_str = f"{score_val:+.1f}" if score_val is not None else "--"
            eps_str = (
                f"{latest_surprise.eps_surprise_pct:+.1%}"
                if latest_surprise.eps_surprise_pct is not None
                else "--%"
            )
            rev_str = (
                f"{latest_surprise.revenue_surprise_pct:+.1%}"
                if latest_surprise.revenue_surprise_pct is not None
                else "--%"
            )
            lines.append(
                f"• {latest_surprise.fiscal_period} 業績評分: {score_icon} **{score_str}** "
                f"(EPS 驚喜: `{eps_str}` ｜ 營收: `{rev_str}`)"
            )

        if guidance is not None:
            tone_val = guidance.tone_delta_score
            tone_icon = "🟢" if tone_val > 10 else ("🔴" if tone_val < -10 else "⚪")
            extra_guidance = ""
            if guidance.data_json:
                try:
                    curr_g = GuidanceExtraction.model_validate_json(guidance.data_json)
                    g_summary = compare_guidance(curr_g)
                    extra_guidance = f" ｜ 指引方向: **{g_summary.verdict}** ({g_summary.margin_trend})"
                except Exception:
                    extra_guidance = ""

            lines.append(
                f"• 管理層前瞻態度: {tone_icon} 語意分數 **{tone_val:+.1f}**"
                f"{extra_guidance} (指引模型: `{guidance.model_version}`)"
            )
        else:
            lines.append("• 管理層前瞻指引: 暫無結構化指引 (待 8-K Exhibit 99.1 擷取)")

        if snapshots:
            q0 = next((s for s in snapshots if s.horizon == "0q"), None)
            q1 = next((s for s in snapshots if s.horizon == "+1q"), None)
            snap_parts: list[str] = []
            if q0:
                snap_parts.append(f"0Q: `${q0.eps_mean:.2f}`")
            if q1:
                snap_parts.append(f"+1Q: `${q1.eps_mean:.2f}`")
            if snap_parts:
                lines.append(f"• 分析師共識中樞: {' ｜ '.join(snap_parts)}")
            else:
                lines.append("• 分析師共識快照: 追蹤中")
        else:
            lines.append("• 分析師共識快照: 待下輪市場共識同步")

        return header, "\n".join(lines)


class ChannelCheckSection:
    """PR4: 實體產業鏈交叉驗證區塊。"""

    @property
    def section_id(self) -> str:
        return "channel_checks"

    async def render(self, symbol: str) -> tuple[str, str]:
        sym_upper = symbol.strip().upper()
        header = "🔗 實體產業鏈交叉驗證 (Channel Checks)"

        # 查詢與該標的相關之產業鏈
        links = get_links_for_symbol(sym_upper)
        if not links:
            return header, "• 產業鏈定位: 未涵蓋於當前 17 條核心產業鏈矩陣中"

        # 讀取資料庫中與該標的相關之最新交叉驗證日誌 (第一筆為最新週期)
        logs = await asyncio.to_thread(get_channel_checks_by_symbol, sym_upper, 20)
        logs_by_key: dict[str, ChannelCheckLogRecord] = {}
        for log in logs:
            if log.link_key not in logs_by_key:
                logs_by_key[log.link_key] = log

        lines: list[str] = []
        for link in links:
            tag = f"[{link.link_type}{' 🧪' if link.experimental else ''}]"
            matched_log = logs_by_key.get(link.link_key)
            if matched_log is not None:
                icon = (
                    "🟢"
                    if matched_log.verdict == "CONFIRM"
                    else ("🔴" if matched_log.verdict == "DIVERGE" else "⚪")
                )
                detail_parts: list[str] = []
                if matched_log.driver_growth is not None:
                    detail_parts.append(f"驅動 {matched_log.driver_growth:+.1f}%")
                if matched_log.follower_growth is not None:
                    detail_parts.append(f"跟隨 {matched_log.follower_growth:+.1f}%")
                if matched_log.divergence_pp is not None:
                    detail_parts.append(f"偏差 {matched_log.divergence_pp:+.1f}pp")
                detail_str = " ｜ ".join(detail_parts) if detail_parts else "數據觀測中"
                lines.append(
                    f"• {tag} {link.link_key}: {icon} **{matched_log.verdict}** ({detail_str})"
                )
            else:
                lines.append(
                    f"• {tag} {link.link_key}: ⚪ **INSUFFICIENT** (待高頻與財務數據更新)"
                )

        return header, "\n".join(lines)


# 全域單例區塊註冊中心
fa_section_registry = FaSectionRegistry()
fa_section_registry.register(MacroLiquiditySection())
fa_section_registry.register(GovernanceGateSection())
fa_section_registry.register(EarningsSurpriseSection())
fa_section_registry.register(ChannelCheckSection())


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
