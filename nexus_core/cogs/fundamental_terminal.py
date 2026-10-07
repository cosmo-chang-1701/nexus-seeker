"""基本面分析管線互動診斷終端 Cog (/fa 指令)。

架構原則：
1. 輸出集中化：嚴格透過 cogs.embed_builders.fundamental_embeds 構建 Embed，Cog 內禁止直接實例化 discord.Embed。
2. 唯讀互動：嚴格設置 ephemeral=True，保護用戶查詢隱私，完全無寫入副作用。
3. 開閉原則：採用 FaSectionRegistry 註冊機制，支援各 PR 漸進式掛載診斷區塊。
4. 零執行不變量：顧問性評級展示，絕無自動下單或平倉。
"""

from __future__ import annotations

import asyncio
import json
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
    get_latest_fair_value,
    get_latest_guidance_extraction,
    get_latest_liquidity_regime,
    get_latest_revision_score,
    get_prior_guidance_extraction,
    get_sec_filing_cursor,
    get_watch_candidates,
)
from discord import app_commands
from discord.ext import commands
from market_analysis.fundamental_pipeline.governance_gate import (
    evaluate_governance_status,
)
from market_analysis.fundamental_pipeline.channel_check import (
    LINK_TYPE_LABELS_ZH,
    NOWCAST_DIRECTION_LABELS_ZH,
    VERDICT_LABELS_ZH as CHANNEL_VERDICT_LABELS_ZH,
)
from market_analysis.fundamental_pipeline.earnings_surprise import FLOOR_EPS
from market_analysis.fundamental_pipeline.guidance_delta import (
    VERDICT_LABELS_ZH,
    compare_guidance,
)
from market_analysis.fundamental_pipeline.insider_signal import (
    evaluate_insider_signal,
)
from market_analysis.fundamental_pipeline.models import (
    ChannelCheckLogRecord,
    EarningsSurpriseDTO,
    EPSEstimateSnapshotRecord,
    GuidanceExtraction,
    GuidanceExtractionDTO,
    SupplyChainLink,
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
        header = "🚨 治理與重大事件監控 (Governance Gate)"

        # SEC 申報同步由平日 07:00–20:00 ET 每整點排程執行，只涵蓋持倉與自選標的池。
        # 從未同步（無游標：尚未輪到首次同步、不在標的池或同步失敗）時不得把「沒有資料」
        # 顯示成「正常」或 NEUTRAL，必須與「已同步且乾淨」明確區分。
        cursor = await asyncio.to_thread(get_sec_filing_cursor, sym_upper)
        if cursor is None:
            no_data = "⚪ 尚無申報同步資料（待排程首次同步；僅涵蓋持倉與自選標的）"
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


_AWAIT_NEXT_EARNINGS_8K = "待下一份財報 8-K 觸發"
_HORIZON_LABELS_ZH: dict[str, str] = {
    "0q": "本季",
    "+1q": "下季",
    "0y": "本財年",
    "+1y": "下財年",
}


def _parse_guidance(record: GuidanceExtractionDTO | None) -> GuidanceExtraction | None:
    if record is None or not record.data_json:
        return None
    try:
        return GuidanceExtraction.model_validate_json(record.data_json)
    except Exception:
        return None


class EarningsSurpriseSection:
    """PR3: 財務預期差與管理層前瞻指引區塊。

    財報預期差由 SEC 申報同步發現 8-K Item 2.02 時事件觸發（僅涵蓋持倉與自選標的），
    沒有資料時必須明示，不可用看似正常的佔位內容。
    """

    @property
    def section_id(self) -> str:
        return "earnings_surprise"

    async def render(self, symbol: str) -> tuple[str, str]:
        sym_upper = symbol.strip().upper()
        header = "📊 業績預期差與財報後漂移"

        latest_surprise = await asyncio.to_thread(
            get_latest_earnings_surprise, sym_upper
        )
        guidance = await asyncio.to_thread(get_latest_guidance_extraction, sym_upper)
        snapshots = await asyncio.to_thread(get_eps_estimate_snapshots, sym_upper)

        if latest_surprise is None and guidance is None and not snapshots:
            return (
                header,
                f"• ⚪ 尚無財報預期差資料（{_AWAIT_NEXT_EARNINGS_8K}；僅涵蓋持倉與自選標的）",
            )

        lines: list[str] = [self._surprise_line(latest_surprise)]
        lines.extend(await self._guidance_lines(sym_upper, guidance))
        lines.append(self._snapshot_line(snapshots))

        # 讀取分析師修正動能與 PEAD
        rev_record = await asyncio.to_thread(get_latest_revision_score, sym_upper)
        if rev_record is not None:
            pead_str = "✅ PEAD_ALIGNED" if rev_record.is_pead_aligned else "⚪ 未共振"
            lines.append(
                f"• 分析師修正動能: **{rev_record.score_30d:+.1f}** ｜ PEAD 共振: {pead_str}"
            )
        return header, "\n".join(lines)

    @staticmethod
    def _surprise_line(latest_surprise: EarningsSurpriseDTO | None) -> str:
        if latest_surprise is None:
            return f"• 業績預期差: ⚪ 尚無財報預期差資料（{_AWAIT_NEXT_EARNINGS_8K}）"

        period = latest_surprise.fiscal_period
        if latest_surprise.status != "PROCESSED":
            cons = (
                f"`${latest_surprise.consensus_eps:.2f}`"
                if latest_surprise.consensus_eps is not None
                else "--"
            )
            return f"• {period} 業績評分: ⏳ 待實際值公布（共識 EPS: {cons}）"

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
        line = (
            f"• {period} 業績評分: {score_icon} **{score_str}** "
            f"(EPS 驚喜: `{eps_str}` ｜ 營收: `{rev_str}`)"
        )
        cons_eps = latest_surprise.consensus_eps
        if cons_eps is not None and abs(cons_eps) < FLOOR_EPS:
            line += (
                f"\n  ⚠️ 小基數：共識 EPS `${cons_eps:.2f}` 絕對值低於 ${FLOOR_EPS:.2f}，"
                "EPS 驚喜百分比以下限為分母，僅供參考"
            )
        return line

    @staticmethod
    async def _guidance_lines(
        sym_upper: str, guidance: GuidanceExtractionDTO | None
    ) -> list[str]:
        if guidance is None:
            return [
                f"• 管理層前瞻指引: ⚪ 尚無指引擷取資料（{_AWAIT_NEXT_EARNINGS_8K}）"
            ]

        curr_g = _parse_guidance(guidance)
        if curr_g is None:
            return ["• 管理層前瞻指引: ⚠️ 指引資料解析失敗"]

        prior_record = await asyncio.to_thread(
            get_prior_guidance_extraction,
            sym_upper,
            guidance.fiscal_period,
            guidance.source_accession,
        )
        prior_g = _parse_guidance(prior_record)
        g_summary = compare_guidance(curr_g, prior_g)

        tone_abs = g_summary.tone_score
        tone_icon = "🟢" if tone_abs > 10 else ("🔴" if tone_abs < -10 else "⚪")
        if g_summary.tone_delta is not None and prior_record is not None:
            delta_str = (
                f"較前期（{prior_record.fiscal_period}）`{g_summary.tone_delta:+.1f}`"
            )
        else:
            delta_str = "無前期指引可比"

        verdict_zh = VERDICT_LABELS_ZH.get(g_summary.verdict, "無法判定")
        verdict_line = f"• 前瞻指引方向: **{verdict_zh}**（{g_summary.margin_trend}）"
        if g_summary.verdict == "UNKNOWN" and g_summary.comparison_note:
            verdict_line += f"\n  ↳ {g_summary.comparison_note}"

        return [
            (
                f"• 管理層前瞻態度（{guidance.fiscal_period} 財報）: {tone_icon} "
                f"語意分數 **{tone_abs:+.1f}** ｜ {delta_str} "
                f"(指引模型: `{guidance.model_version}`)"
            ),
            verdict_line,
        ]

    @staticmethod
    def _snapshot_line(snapshots: list[EPSEstimateSnapshotRecord]) -> str:
        if not snapshots:
            return f"• 分析師共識快照: ⚪ 尚無共識快照資料（{_AWAIT_NEXT_EARNINGS_8K}）"
        snap_parts: list[str] = []
        for horizon in ("0q", "+1q"):
            snap = next((s for s in snapshots if s.horizon == horizon), None)
            if snap is not None:
                snap_parts.append(
                    f"{_HORIZON_LABELS_ZH[horizon]}: `${snap.eps_mean:.2f}`"
                )
        if not snap_parts:
            return "• 分析師共識快照: ⚪ 無季度共識預估"
        snap_date = snapshots[0].snapshot_date
        return f"• 分析師共識 EPS（{snap_date}）: {' ｜ '.join(snap_parts)}"


_CHANNEL_SECTION_CHAR_BUDGET = 950  # Discord 欄位上限 1024 字元，保留結尾空行餘裕
_CHANNEL_REASON_MAX_CHARS = 60
_CHANNEL_NO_DATA = "尚無產業鏈檢驗資料（待每日 18:00 ET 排程寫入）"


def _pick_channel_log(
    logs: list[ChannelCheckLogRecord],
) -> ChannelCheckLogRecord | None:
    """同鏈多期時優先取最新的非「資料不足」結果；全為資料不足時取最新一期。"""
    for log in logs:
        if log.verdict != "INSUFFICIENT":
            return log
    return logs[0] if logs else None


def _channel_reason(log: ChannelCheckLogRecord) -> str:
    try:
        members = json.loads(log.members_json) if log.members_json else {}
    except (ValueError, TypeError):
        return ""
    text = str(members.get("summary_text", "")) if isinstance(members, dict) else ""
    if "：" in text:
        text = text.split("：", 1)[1]
    if len(text) > _CHANNEL_REASON_MAX_CHARS:
        text = text[: _CHANNEL_REASON_MAX_CHARS - 1] + "…"
    return text


def _format_channel_line(
    link: SupplyChainLink, log: ChannelCheckLogRecord | None
) -> str:
    type_zh = LINK_TYPE_LABELS_ZH.get(link.link_type, "產業鏈")
    tag = f"{type_zh}{'・🧪實驗性' if link.experimental else ''}"
    if log is None:
        return f"• {link.title}（{tag}）: ⚪ 尚無檢驗紀錄"
    icon = {"CONFIRM": "🟢", "DIVERGE": "🔴"}.get(log.verdict, "⚪")
    verdict_zh = CHANNEL_VERDICT_LABELS_ZH.get(log.verdict, "資料不足")
    parts: list[str] = []
    if log.driver_growth is not None:
        inverse = "（反向）" if link.polarity == -1 else ""
        parts.append(f"驅動{inverse} {log.driver_growth:+.1f}%")
    if log.follower_growth is not None:
        parts.append(f"跟隨 {log.follower_growth:+.1f}%")
    if log.divergence_pp is not None:
        parts.append(f"偏差 {log.divergence_pp:+.1f}pp")
    if log.nowcast_direction is not None:
        parts.append(NOWCAST_DIRECTION_LABELS_ZH.get(log.nowcast_direction, ""))
    if log.verdict == "INSUFFICIENT":
        reason = _channel_reason(log)
        if reason:
            parts.append(reason)
    detail = " ｜ ".join(p for p in parts if p)
    detail_str = f"（{detail}）" if detail else ""
    return f"• {link.title}（{tag}）{log.as_of_period}: {icon} **{verdict_zh}**{detail_str}"


class ChannelCheckSection:
    """PR4: 實體產業鏈交叉驗證區塊。

    資料由事件時鐘每日 18:00 ET（NYSE 交易日）排程寫入 channel_check_log；
    排程尚未寫入時必須明示「尚無資料」，不得顯示成看似判定結果的「資料不足」。
    """

    @property
    def section_id(self) -> str:
        return "channel_checks"

    async def render(self, symbol: str) -> tuple[str, str]:
        sym_upper = symbol.strip().upper()
        header = "🔗 實體產業鏈交叉驗證"

        links = get_links_for_symbol(sym_upper)
        if not links:
            return header, "• 產業鏈定位: 未涵蓋於當前 17 條核心產業鏈矩陣中"

        # 讀取資料庫中與該標的相關之交叉驗證日誌（依期別新到舊）
        logs = await asyncio.to_thread(get_channel_checks_by_symbol, sym_upper, 60)
        if not logs:
            titles = "、".join(link.title for link in links)
            return header, f"• ⚪ {_CHANNEL_NO_DATA}\n• 涵蓋產業鏈: {titles}"

        logs_by_key: dict[str, list[ChannelCheckLogRecord]] = {}
        for log in logs:
            logs_by_key.setdefault(log.link_key, []).append(log)

        lines: list[str] = []
        used = 0
        for idx, link in enumerate(links):
            line = _format_channel_line(
                link, _pick_channel_log(logs_by_key.get(link.link_key, []))
            )
            if used + len(line) + 1 > _CHANNEL_SECTION_CHAR_BUDGET:
                lines.append(f"• …其餘 {len(links) - idx} 條產業鏈略")
                break
            lines.append(line)
            used += len(line) + 1

        return header, "\n".join(lines)


class ValuationEngineSection:
    """PR5: 內在價值與安全邊際 (Valuation Engine) 區塊。"""

    @property
    def section_id(self) -> str:
        return "valuation_engine"

    async def render(self, symbol: str) -> tuple[str, str]:
        sym_upper = symbol.strip().upper()
        header = "💰 內在價值與安全邊際 (Valuation Engine)"

        fv_record = await asyncio.to_thread(get_latest_fair_value, sym_upper)
        if fv_record is None or fv_record.fair_value <= 0:
            return (
                header,
                "• 公允價值: 暫無可用現金流或同業倍數數據 (待估值時鐘排定同步)",
            )

        dcf_str = (
            f"${fv_record.dcf_value:.2f}"
            if fv_record.dcf_value is not None
            else "無效/退回"
        )
        comps_str = (
            f"${fv_record.comps_value:.2f}"
            if fv_record.comps_value is not None
            else "無效/同業不足"
        )
        mos = fv_record.margin_of_safety

        if mos >= 0.25:
            val_tag = "🟢 深度折價 (安全邊際充足)"
        elif mos >= 0.10:
            val_tag = "🟢 中度折價"
        elif mos >= -0.10:
            val_tag = "⚪ 合理估值區間"
        else:
            val_tag = "🔴 明顯溢價"

        lines = [
            f"• 公允價值中樞: **${fv_record.fair_value:.2f}** (DCF: `{dcf_str}` ｜ 同業倍數: `{comps_str}`)",
            f"• 安全邊際 (MOS): **{mos:+.2%}** ({val_tag})",
            f"• 權益資本成本: `{fv_record.discount_rate:.2%}` (隱含 ERP: `{fv_record.equity_risk_premium:.2%}`)",
        ]
        return header, "\n".join(lines)


class WatchCandidateSection:
    """PR5: 基本面次日候選名單與時鐘映射區塊。"""

    @property
    def section_id(self) -> str:
        return "watch_candidate"

    async def render(self, symbol: str) -> tuple[str, str]:
        sym_upper = symbol.strip().upper()
        header = "📋 綜合評級與時鐘映射"

        candidates = await asyncio.to_thread(get_watch_candidates)
        cand = next((c for c in candidates if c.symbol == sym_upper), None)

        if cand is None:
            return (
                header,
                (
                    "• 次日排定狀態: ⚪ 候選池常態輪巡中 (待盤後 20:00 估值時鐘排定)\n"
                    "• 顧問說明: 標的處於基本面池日常覆蓋，未觸發重大風控排除。"
                ),
            )

        if cand.status == "EXCLUDED":
            return (
                header,
                (
                    f"• 次日排定狀態: ⛔ **EXCLUDED (風控排除)** (排名: #{cand.rank})\n"
                    f"• 排除原因: {cand.excluded_reason or '未符合基本面池準入標準'}"
                ),
            )

        status_icon = "🎯" if cand.status == "CANDIDATE" else "👀"
        reasons_detail = ""
        if cand.reasons_json:
            try:
                r_dict = json.loads(cand.reasons_json)
                c_score = r_dict.get("composite_score")
                mos_val = r_dict.get("margin_of_safety")
                pead_val = r_dict.get("pead_aligned")
                pead_str = "✅ PEAD 共振" if pead_val else "⚪ 未共振"
                score_str = f"{c_score:+.1f}" if c_score is not None else "--"
                mos_str = f"{mos_val:+.1%}" if mos_val is not None else "--%"
                reasons_detail = f"\n• 綜合評估分: **{score_str}** (安全邊際: `{mos_str}` ｜ {pead_str})"
            except Exception:
                pass

        return (
            header,
            (
                f"• 次日排定狀態: {status_icon} **{cand.status}** (排名: #{cand.rank})"
                f"{reasons_detail}"
            ),
        )


# 全域單例區塊註冊中心
fa_section_registry = FaSectionRegistry()
fa_section_registry.register(MacroLiquiditySection())
fa_section_registry.register(GovernanceGateSection())
fa_section_registry.register(EarningsSurpriseSection())
fa_section_registry.register(ChannelCheckSection())
fa_section_registry.register(ValuationEngineSection())
fa_section_registry.register(WatchCandidateSection())


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
