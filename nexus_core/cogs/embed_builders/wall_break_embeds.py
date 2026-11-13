"""GEX 牆突破／跌破確認推播 Embed。

只渲染「突破候選／跌破候選」（盤中 CANDIDATE）與「確認突破／確認跌破」（T+1 CONFIRMED）；
FAKE／UNDETERMINED 僅記錄不推播。門檻尚未校準，文案一律不稱「真突破」。
"""

from typing import Sequence

import discord

from cogs.embed_builders._ansi_utils import _visual_len, _visual_truncate
from cogs.embed_builders._core import NexusEmbed
from market_analysis.wall_break import (
    RETEST_STOP_PCT_MAX,
    RETEST_STOP_PCT_MIN,
    GateResult,
    T1EventInput,
    T1Result,
    WallBreakResult,
)
from market_analysis.squeeze_entry.rules import TIER_SIZE_PCT

_ICON = {"PASS": "✅", "FAIL": "❌", "UNKNOWN": "❔"}
_STATUS_ANSI = {"PASS": "1;32", "FAIL": "1;31", "UNKNOWN": "1;33"}
_CANDIDATE_PREFIX = "（候選，待 T+1 確認；次日未收到確認推播即視為未成立）"
_UOA_NOTE = "※ UOA 方向為日累積推斷（非逐筆成交方向）"
_RESET = "\u001b[0m"
_DETAIL_MAX_VLEN = 70
# 右側現貨輕倉比例比照擠壓進場 T1–T3（取自 TIER_SIZE_PCT，避免與規格漂移）
_SIZE_RANGE = f"{min(TIER_SIZE_PCT.values()):g}%–{max(TIER_SIZE_PCT.values()):g}%"


def _tree(lines: Sequence[str]) -> str:
    """以 ├─／└─ 組成樹狀文字（最後一行用 └─）。"""
    last = len(lines) - 1
    return "\n".join(
        f" {'└─' if i == last else '├─'} {line}" for i, line in enumerate(lines)
    )


def _ansi_block(lines: Sequence[str], head: str = "") -> str:
    body = (head + "\n" if head else "") + _tree(lines)
    return f"```ansi\n{body}\n```"


def _val(text: str) -> str:
    return f"\u001b[1;37m{text}{_RESET}"


def _truncate_detail(text: str) -> str:
    """超過顯示寬度上限時截斷並補「…」，結果寬度不超過上限。"""
    if _visual_len(text) <= _DETAIL_MAX_VLEN:
        return text
    return _visual_truncate(text, _DETAIL_MAX_VLEN - _visual_len("…")) + "…"


def _gate_lines(gates: Sequence[GateResult]) -> list[str]:
    return [
        f"{_ICON[g.status]} {g.label}："
        f"\u001b[{_STATUS_ANSI[g.status]}m"
        f"{_truncate_detail(g.detail)}{_RESET}"
        for g in gates
    ]


def _directive_lines(direction: str, wall: float) -> list[str]:
    if direction == "BREAKOUT":
        lo = wall * (1 - RETEST_STOP_PCT_MAX)
        hi = wall * (1 - RETEST_STOP_PCT_MIN)
        return [
            "嚴禁追高現貨與在突破點賣低履約價 Covered Call。",
            f"等待縮量回踩原 CallWall ${wall:.2f} 確認支撐後，"
            f"以總資產 {_SIZE_RANGE} 輕倉配置右側現貨（比照擠壓進場 T1–T3），"
            f"結構停損設於 ${lo:.2f}–${hi:.2f}；",
            "或於原底牆賣 30–45 DTE、Delta 0.12–0.15 的現金擔保賣 Put（CSP）。",
        ]
    return [
        "嚴禁左側接刀，嚴禁在負 GEX 助跌區賣任何 CSP。",
        "保持 100% 現金／BOXX，待恐慌出清、VIX ≥ 22 且下方出現新的正 GEX 承接底牆後，",
        "才進入評估。",
    ]


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
        color=discord.Color.green() if up else discord.Color.red(),
    )
    wall_name = "CallWall" if up else "PutWall"
    embed.add_field(
        name="🎯 觸發事件",
        value=_ansi_block(
            [
                f"K 棒時間：{_val(t.bar_time.strftime('%m-%d %H:%M') + ' ET')}",
                f"收盤價：{_val(f'${t.close:.2f}')}",
                f"前日 {wall_name}：{_val(f'${t.old_wall:.2f}')}",
                f"錨點日：{_val(str(t.anchor_trade_date))}",
            ]
        ),
        inline=False,
    )
    embed.add_field(
        name="🧪 判定關卡",
        value=_ansi_block(_gate_lines(result.gates)) + "\n" + _UOA_NOTE,
        inline=False,
    )
    embed.add_field(
        name="📋 操作指引",
        value=_ansi_block(
            _directive_lines(t.direction, t.old_wall),
            head=f"\u001b[1;33m{_CANDIDATE_PREFIX}{_RESET}",
        ),
        inline=False,
    )
    embed.set_footer(
        text=f"門檻未校準（PRE_CALIBRATION）｜錨點＝{t.anchor_trade_date} 收盤牆"
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
        color=discord.Color.green() if up else discord.Color.red(),
    )
    wall_name = "CallWall" if up else "PutWall"
    embed.add_field(
        name="🎯 觸發事件",
        value=_ansi_block(
            [
                f"事件日：{_val(event_trade_date)}",
                f"舊牆 {wall_name}：{_val(f'${event.old_wall:.2f}')}",
            ]
        ),
        inline=False,
    )
    embed.add_field(
        name="🧪 T+1 定案檢核",
        value=_ansi_block(_gate_lines(result.checks)),
        inline=False,
    )
    embed.add_field(
        name="📋 操作指引",
        value=_ansi_block(_directive_lines(event.direction, event.old_wall)),
        inline=False,
    )
    embed.set_footer(text=f"門檻未校準（PRE_CALIBRATION）｜事件日 {event_trade_date}")
    return embed
