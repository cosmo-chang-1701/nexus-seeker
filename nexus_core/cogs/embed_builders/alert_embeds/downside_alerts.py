"""投組下行風險警報與快照欄位（Sortino / MDD / VaR / CVaR）。"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Literal, Optional, Sequence

import discord

from cogs.embed_builders._core import NexusEmbed
from market_analysis.downside_monitor import (
    CVAR_EXPANSION_RATIO,
    DRAWDOWN_REARM_BUFFER,
    DownsideSnapshot,
)

_CVAR_REASON_TEXT = {
    "BUDGET": "1 日 CVaR95 超過帳戶風險預算",
    "EXPANSION": f"近一季尾部損失較過去 60 日基準擴張 ≥ {CVAR_EXPANSION_RATIO:.1f} 倍",
}


def _pct(value: Optional[float]) -> str:
    return "N/A" if value is None else f"{value * 100:.2f}%"


def _ratio(value: Optional[float]) -> str:
    return "N/A" if value is None else f"{value:.2f}"


def create_downside_snapshot_fields(
    simulated: Optional[DownsideSnapshot],
    realized: Optional[DownsideSnapshot] = None,
) -> list[tuple[str, str, bool]]:
    """下行風險快照欄位 `(name, value, inline)`，供盤後戰報、VTR 週報共用。

    Sortino 為主要評估指標，列在第一行；MDD 與 VaR / CVaR 為輔。模擬值 = 目前部位
    回推一年；已實現值 = `portfolio_nav_daily` 逐日快照（累積 ≥ 60 個交易日後才顯示）。
    """
    if simulated is None:
        return [
            (
                "📉 投組下行風險 (Sortino / MDD / CVaR)",
                "目前無持倉或歷史資料不足，無法計算。",
                False,
            )
        ]

    def _block(snap: DownsideSnapshot) -> str:
        return "\n".join(
            [
                f"• **Sortino（主）**：一季 `{_ratio(snap.sortino_63)}`｜一年 `{_ratio(snap.sortino_252)}`",
                f"• 最大回撤 (MDD)：`{_pct(snap.max_drawdown)}`｜距高點 `{_pct(snap.current_drawdown)}`",
                f"• 1 日 VaR95：`{_pct(snap.var_95)}`｜CVaR95：`{_pct(snap.cvar_95)}`",
            ]
        )

    fields = [
        (
            f"📉 投組下行風險・現部位回推一年（{simulated.n_obs} 個交易日）",
            _block(simulated),
            False,
        )
    ]
    if realized is not None:
        fields.append(
            (
                f"📒 投組下行風險・已實現淨值（{realized.n_obs} 個交易日）",
                _block(realized),
                False,
            )
        )
    return fields


def create_portfolio_downside_alert_embed(
    kind: Literal["DRAWDOWN", "CVAR"],
    snapshot: DownsideSnapshot,
    *,
    tier: Optional[float] = None,
    reasons: Sequence[str] = (),
    budget: Optional[float] = None,
) -> discord.Embed:
    """投組下行風險警報：回撤跨越階梯，或 CVaR 超出預算 / 尾部體制轉換。"""
    if kind == "DRAWDOWN":
        tier_pct = (tier or 0.0) * 100
        embed = NexusEmbed(
            title=f"📉 警報：投組回撤跨越 -{tier_pct:.0f}%",
            description=(
                f"以目前部位回推，淨值距一年高點已回撤 **{_pct(snapshot.current_drawdown)}**。"
                "這是左尾防護訊號：請檢視是否有部位的投資論點已經改變，而非機械式減碼。"
            ),
            color=discord.Color.dark_red()
            if (tier or 0.0) >= 0.15
            else discord.Color.orange(),
            timestamp=datetime.now(timezone.utc),
        )
        embed.add_field(
            name="🔁 重新武裝",
            value=(
                f"回撤回升到 -{max(tier_pct - DRAWDOWN_REARM_BUFFER * 100, 0):.1f}% 之上後，"
                "此階梯才會再次提醒。"
            ),
            inline=False,
        )
    else:
        lines = [f"• {_CVAR_REASON_TEXT.get(r, r)}" for r in reasons]
        embed = NexusEmbed(
            title="📉 警報：投組尾部風險 (CVaR) 升高",
            description="\n".join(lines) or "尾部風險指標觸發。",
            color=discord.Color.orange(),
            timestamp=datetime.now(timezone.utc),
        )
        embed.add_field(
            name="📏 1 日 CVaR95 / 預算",
            value=f"`{_pct(snapshot.cvar_95)}` / `{_pct(budget)}`",
            inline=True,
        )
        embed.add_field(
            name="🌡️ 近一季 CVaR95 / 60 日基準",
            value=f"`{_pct(snapshot.cvar_short)}` / `{_pct(snapshot.cvar_short_baseline)}`",
            inline=True,
        )
    for name, value, inline in create_downside_snapshot_fields(snapshot):
        embed.add_field(name=name, value=value, inline=inline)
    embed.set_footer(text="Nexus Seeker | 投組下行風險監控 (Sortino / MDD / CVaR)")
    return embed
