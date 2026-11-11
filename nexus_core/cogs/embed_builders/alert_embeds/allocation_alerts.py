"""配置偏離提醒推播（docs/strategies/09_static_allocation_rebalance.md §7）。"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional

import discord

from cogs.embed_builders._core import NexusEmbed
from cogs.embed_builders._embed_helpers import (
    _ANSI_0,
    _ANSI_W,
    _add_ansi_field_safely,
    _ansi_tree_lines,
)
from market_analysis.allocation_drift import AllocationBreakdown, ReminderReason

_FOOTER = "配置偏離提醒｜未計入：期權、空頭部位"
_DESCRIPTIONS: dict[ReminderReason, str] = {
    "CROSSING": "股票占比偏離目標已達 5 pp 門檻",
    "MONTHLY": "每月提醒：持倉仍偏離目標",
}


def create_allocation_drift_embed(
    b: AllocationBreakdown,
    reason: ReminderReason,
    macro_state: Optional[str],
    next_withdrawal_date: Optional[str],
) -> discord.Embed:
    """固定比例偏離提醒；總經狀態僅作註記，不影響是否推播。"""
    embed = NexusEmbed(
        title="⚖️ 配置偏離提醒",
        description=_DESCRIPTIONS[reason],
        color=discord.Color.orange(),
        timestamp=datetime.now(timezone.utc),
    )
    _add_ansi_field_safely(
        embed,
        "📊 目前 vs 目標",
        _ansi_tree_lines(
            [
                f"股票 {_ANSI_W}{b.equity_pct:.1f}%{_ANSI_0} → 目標 {b.target_pct:.0f}%"
                f"（偏離 {b.drift_pp:+.1f} pp）",
                f"股票市值：{_ANSI_W}${b.equity_value:,.0f}{_ANSI_0}",
                f"BOXX：{_ANSI_W}${b.boxx_value:,.0f}{_ANSI_0}",
                f"現金：{_ANSI_W}${b.cash:,.0f}{_ANSI_0}",
            ]
        ),
    )
    amount = abs(round(b.move_amount))
    if b.move_amount > 0:
        advice = f"自 BOXX／現金移入股票約 ${amount:,.0f}"
    else:
        advice = f"自股票移到 BOXX 約 ${amount:,.0f}"
    lines = [advice]
    if b.reserve > 0:
        lines.append(
            f"已預留下次提領 ${b.reserve:,.0f}（{next_withdrawal_date or '日期未定'}），"
            "不計入偏離"
        )
    lines.append(
        f"總經確認：{macro_state or '無資料'}（固定比例不依總經調整，僅供參考）"
    )
    embed.add_field(name="🧭 建議", value="\n".join(lines), inline=False)
    embed.set_footer(text=_FOOTER)
    return embed
