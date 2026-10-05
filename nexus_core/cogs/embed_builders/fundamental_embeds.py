"""基本面分析管線專屬 Discord Embed 構建器。

嚴格遵守輸出集中化規範：
- 統一透過 NexusEmbed 構建所有 Embed 物件。
- 遵循 100% 繁體中文標準。
- 零交易執行顧問性聲明。
"""

from __future__ import annotations

from datetime import datetime, timezone
import json
from typing import Sequence
from zoneinfo import ZoneInfo
import discord

from cogs.embed_builders._core import NexusEmbed
from market_analysis.fundamental_pipeline.models import GovernanceFlagRecord

_ET_ZONE = ZoneInfo("America/New_York")


def build_fa_terminal_embed(
    symbol: str,
    company_name: str,
    sections: Sequence[tuple[str, str]],
    footer_note: str | None = None,
) -> NexusEmbed:
    """構建 /fa 互動診斷終端的全景 Embed 視圖。"""
    now_et = datetime.now(timezone.utc).astimezone(_ET_ZONE)
    now_et_str = now_et.strftime("%Y-%m-%d %H:%M")

    sym_upper = symbol.strip().upper()
    title = f"🌌 NEXUS SEEKER | 基本面事件與全景估值雷達: {sym_upper}"
    description = f"標的資產: {company_name} ｜ 資料時效: {now_et_str} ET\n\u200b"

    embed = NexusEmbed(
        title=title,
        description=description,
        color=discord.Color.blue(),
    )

    for header, content in sections:
        # 確保結尾具備空行美化
        val = content.strip()
        if not val.endswith("\u200b"):
            val = f"{val}\n\u200b"
        embed.add_field(name=header, value=val, inline=False)

    footer_text = (
        footer_note if footer_note else "基本面管線純顧問診斷 • 零自動交易執行"
    )
    embed.set_footer(text=footer_text)
    return embed


def build_governance_flag_embed(
    symbol: str,
    flag: GovernanceFlagRecord,
) -> NexusEmbed:
    """構建重大治理風險（CRITICAL / HIGH）推播警訊 Embed。"""
    sym_upper = symbol.strip().upper()
    is_critical = flag.severity == "CRITICAL"

    title = f"🚨 【SEC 治理風控重大警訊】持倉標的觸發強制審查: {sym_upper}"
    color = discord.Color.red() if is_critical else discord.Color.orange()

    embed = NexusEmbed(
        title=title,
        description=f"標的 `{sym_upper}` 於 SEC EDGAR 申報中觸發監管審查紅旗。\n\u200b",
        color=color,
    )

    sev_icon = "🔴" if is_critical else "🟠"
    embed.add_field(
        name="🚨 審查等級與類型",
        value=f"{sev_icon} **{flag.severity}** ｜ 代號: `{flag.flag_kind}`\n有效審查期至: `{flag.expires_at}`\n\u200b",
        inline=False,
    )

    # 解析事由摘錄
    snippet = "未提供詳細文字"
    if flag.detail_json:
        try:
            detail_dict = json.loads(flag.detail_json)
            snippet = str(
                detail_dict.get("snippet") or detail_dict.get("title") or snippet
            )
        except Exception:
            snippet = flag.detail_json[:300]

    embed.add_field(
        name="📄 核心事由摘錄",
        value=f"```{snippet[:800]}\n```\n\u200b",
        inline=False,
    )

    embed.add_field(
        name="🛡️ 顧問處置建議",
        value=(
            "已啟動原型假設破滅防禦機制。請檢視多頭部位與現貨曝險。\n"
            "※ 本提示為純顧問性風控評級，絕無自動下單、平倉或對沖處置。"
        ),
        inline=False,
    )

    embed.set_footer(text="SEC 申報直連流 • 治理風控閘門")
    return embed
