"""提領跑道推播：壓力跑道警示與提領提醒（docs/risk_portfolio/05 階段三）。"""

from __future__ import annotations

from datetime import date, datetime, timezone
from typing import Literal

import discord

from cogs.embed_builders._core import NexusEmbed
from cogs.embed_builders._embed_helpers import format_runway_lines
from database.withdrawal_runway import RunwaySnapshot
from market_analysis.withdrawal_runway import (
    RUNWAY_REARM_BUFFER_YEARS,
    WithdrawalPlan,
)

_FOOTER = "Nexus Seeker | 提領跑道 (歷史壓力重演)"


def create_runway_warning_embed(snap: RunwaySnapshot, tier: int) -> discord.Embed:
    """壓力跑道跌破 `tier` 年門檻的警示。"""
    embed = NexusEmbed(
        title=f"🏦 警報：壓力跑道跌破 {tier} 年",
        description=(
            f"若歷史崩跌從今天重演，依目前提領額只能再提領 "
            f"**{snap.stress_years:.1f} 年**。系統不代為調整：可考慮少提、延後提領，"
            "或補入資金。"
        ),
        color=discord.Color.dark_red() if tier <= 1 else discord.Color.orange(),
        timestamp=datetime.now(timezone.utc),
    )
    embed.add_field(
        name="📊 目前跑道", value="\n".join(format_runway_lines(snap)), inline=False
    )
    embed.add_field(
        name="🔁 重新武裝",
        value=f"壓力跑道回升到 {tier + RUNWAY_REARM_BUFFER_YEARS:.1f} 年以上後，此門檻才會再次提醒。",
        inline=False,
    )
    embed.set_footer(text=_FOOTER)
    return embed


def create_withdrawal_reminder_embed(
    kind: Literal["PRE", "DAY"],
    target_date: date,
    plan: WithdrawalPlan,
    snap: RunwaySnapshot,
    *,
    rebalance: bool = False,
) -> discord.Embed:
    """提領提醒：`PRE` = 前一個月 15 日的前置提醒，`DAY` = 提領月份首個交易日。"""
    when = target_date.isoformat()
    title = (
        f"🏦 提領前置提醒：{when} 提領" if kind == "PRE" else f"🏦 提領日提醒：{when}"
    )
    amount_line = f"本次提領 **${plan.amount:,.0f}**" + (
        "（未含通膨調整：尚無 CPI 資料）" if snap.cpi_missing else "（已含通膨調整）"
    )
    desc = [amount_line]
    if rebalance:
        desc.append("本次提領兼作年度再平衡，不另外產生交易建議。")
    embed = NexusEmbed(
        title=title,
        description="\n".join(desc),
        color=discord.Color.blue(),
        timestamp=datetime.now(timezone.utc),
    )

    sell_lines = [f"• BOXX：`${plan.from_boxx:,.0f}`"] if plan.from_boxx > 0 else []
    for sym, value in sorted(plan.sells.items(), key=lambda kv: kv[1], reverse=True):
        sell_lines.append(f"• {sym}：`${value:,.0f}`")
    if not sell_lines:
        sell_lines.append("• 無可賣出部位")
    if plan.shortfall > 0:
        sell_lines.append(f"⚠️ 全部可賣部位仍不足，差額 `${plan.shortfall:,.0f}`")
    embed.add_field(
        name="💵 賣出清單（先用 BOXX，不足者賣超配最多的持股）",
        value="\n".join(sell_lines),
        inline=False,
    )
    embed.add_field(
        name="📊 目前跑道", value="\n".join(format_runway_lines(snap)), inline=False
    )
    embed.set_footer(text=f"{_FOOTER}｜依 {snap.nav_date} 收盤淨值與價格計算，僅供參考")
    return embed
