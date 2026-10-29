"""GEX 牆突破／跌破確認推播 Embed。

只渲染「突破候選／跌破候選」（盤中 CANDIDATE）與「確認突破／確認跌破」（T+1 CONFIRMED）；
FAKE／UNDETERMINED 僅記錄不推播。門檻尚未校準，文案一律不稱「真突破」。
"""

from typing import Sequence

import discord

from cogs.embed_builders._core import NexusEmbed
from market_analysis.wall_break import (
    RETEST_STOP_PCT_MAX,
    RETEST_STOP_PCT_MIN,
    GateResult,
    T1EventInput,
    T1Result,
    WallBreakResult,
)

_ICON = {"PASS": "✅", "FAIL": "❌", "UNKNOWN": "❔"}
_CANDIDATE_PREFIX = "（候選，待 T+1 確認；次日未收到確認推播即視為未成立）"


def _gate_lines(gates: Sequence[GateResult]) -> str:
    return "\n".join(f"{_ICON[g.status]} {g.label}：{g.detail}" for g in gates)


def _directive(direction: str, wall: float) -> str:
    if direction == "BREAKOUT":
        lo = wall * (1 - RETEST_STOP_PCT_MAX)
        hi = wall * (1 - RETEST_STOP_PCT_MIN)
        return (
            "嚴禁追高現貨與在突破點賣低履約價 Covered Call。"
            f"等待縮量回踩原 CallWall ${wall:.2f} 確認支撐後，"
            "以 10%–15% 輕倉配置右側現貨，"
            f"結構停損設於 ${lo:.2f}–${hi:.2f}；"
            "或於原底牆賣 30–45 DTE、Delta 0.12–0.15 的現金擔保賣 Put（CSP）。"
        )
    return (
        "嚴禁左側接刀，嚴禁在負 GEX 助跌區賣任何 CSP。"
        "保持 100% 現金／BOXX，待恐慌出清、VIX ≥ 22 且下方出現新的正 GEX 承接底牆後，"
        "才進入評估。"
    )


def create_wall_break_embed(symbol: str, result: WallBreakResult) -> NexusEmbed:
    if result.verdict != "CANDIDATE":
        raise ValueError("僅 CANDIDATE 結果可推播")
    t = result.trigger
    up = t.direction == "BREAKOUT"
    if up:
        title = (
            f"🧱 {symbol} 突破前日 CallWall ${t.old_wall:.2f}｜突破候選（待 T+1 確認）"
        )
    else:
        title = (
            f"🧱 {symbol} 跌破前日 PutWall ${t.old_wall:.2f}｜跌破候選（待 T+1 確認）"
        )
    embed = NexusEmbed(
        title=title,
        description=(
            f"K 棒 {t.bar_time.strftime('%m-%d %H:%M')} ET｜收盤 ${t.close:.2f}｜"
            f"錨點日 {t.anchor_trade_date}\n\n{_gate_lines(result.gates)}"
        ),
        color=discord.Color.green() if up else discord.Color.red(),
    )
    embed.add_field(
        name="🎯 操作指引",
        value=_CANDIDATE_PREFIX + _directive(t.direction, t.old_wall),
        inline=False,
    )
    embed.set_footer(
        text=f"門檻未校準（PRE_CALIBRATION）｜錨點＝{t.anchor_trade_date} 收盤牆"
        "｜UOA 方向為日累積推斷"
    )
    return embed


def create_wall_break_t1_embed(
    symbol: str, event_trade_date: str, event: T1EventInput, result: T1Result
) -> NexusEmbed:
    if result.verdict != "CONFIRMED":
        raise ValueError("僅 CONFIRMED 結果可推播")
    up = event.direction == "BREAKOUT"
    if up:
        title = f"🧱 {symbol} T+1 定案｜確認突破（CallWall ${event.old_wall:.2f}）"
    else:
        title = f"🧱 {symbol} T+1 定案｜確認跌破（PutWall ${event.old_wall:.2f}）"
    embed = NexusEmbed(
        title=title,
        description=(
            f"事件日 {event_trade_date}｜舊牆 ${event.old_wall:.2f}\n\n"
            f"{_gate_lines(result.checks)}"
        ),
        color=discord.Color.green() if up else discord.Color.red(),
    )
    embed.add_field(
        name="🎯 操作指引",
        value=_directive(event.direction, event.old_wall),
        inline=False,
    )
    embed.set_footer(text=f"門檻未校準（PRE_CALIBRATION）｜事件日 {event_trade_date}")
    return embed
