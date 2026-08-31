"""巨觀環境掃描與 FOMC 逃頂窗口 Embed 建構函式。"""

import discord

from datetime import datetime, timezone
from typing import Any, List, Optional

from cogs.embed_builders._ansi_utils import _pad_string
from cogs.embed_builders._core import NexusEmbed


def create_macro_scan_embed(
    macro_data: dict, alerts: Optional[List[Any]] = None
) -> discord.Embed:
    """建立巨觀環境與隔夜市場掃描 Embed (繁體中文)"""
    base_color = discord.Color.red() if alerts else discord.Color.blue()
    embed = NexusEmbed(
        title="🌍 巨觀環境與隔夜市場掃描 (Macro Scan)",
        color=base_color,
        timestamp=datetime.now(timezone.utc),
    )

    dxy = macro_data.get("dxy", 0.0)
    tnx = macro_data.get("tnx", 0.0)
    tnx_change = macro_data.get("tnx_change_bps", 0.0)
    us2y = macro_data.get("us2y", 0.0)
    vix = macro_data.get("vix", 0.0)
    vix_change = macro_data.get("vix_change", 0.0)
    spread = tnx - us2y

    # Consolidate into monospace table
    vix_emoji = "🔥" if vix > 25 else ("⚠️" if vix > 20 else "🟢")

    lines = ["```ansi"]
    headers = ["指標", "數值", "變動 / 備註"]
    widths = [14, 8, 14]
    lines.append(
        " | ".join(
            _pad_string(h, w, "left" if i == 0 else "right")
            for i, (h, w) in enumerate(zip(headers, widths))
        )
    )
    lines.append("-" * (sum(widths) + 3 * (len(widths) - 1)))

    # 1. DXY
    lines.append(
        f"{_pad_string('DXY 美元指數', widths[0])} | {_pad_string(f'{dxy:.2f}', widths[1], 'right')} | {_pad_string('-', widths[2], 'right')}"
    )
    # 2. TNX
    lines.append(
        f"{_pad_string('TNX 10Y 公債', widths[0])} | {_pad_string(f'{tnx:.2f}%', widths[1], 'right')} | {_pad_string(f'{tnx_change:+.1f} bps', widths[2], 'right')}"
    )
    # 3. US2Y
    lines.append(
        f"{_pad_string('US2Y 2Y 公債', widths[0])} | {_pad_string(f'{us2y:.2f}%', widths[1], 'right')} | {_pad_string(f'利差 {spread:+.2f}%', widths[2], 'right')}"
    )
    # 4. VIX
    vix_color_start = (
        "\u001b[0;31m" if vix > 25 else ("\u001b[0;33m" if vix > 20 else "\u001b[0;32m")
    )
    vix_note = f"{vix_change:+.2f} ({vix_emoji})"
    vix_note_colored = f"{vix_color_start}{vix_change:+.2f}\u001b[0m ({vix_emoji})"
    vix_val_str = f"{vix:.2f}"
    lines.append(
        f"{_pad_string('VIX 恐慌指數', widths[0])} | {_pad_string(vix_val_str, widths[1], 'right')} | {_pad_string(vix_note, widths[2], 'right').replace(vix_note, vix_note_colored)}"
    )
    lines.append("```")

    embed.add_field(name="🌍 巨觀數據指標", value="\n".join(lines), inline=False)

    # 結論與警示
    if alerts:
        alert_text = "\n".join([f"• {a}" for a in alerts])
        embed.add_field(
            name="🚨 風險警示 (Macro Alerts)", value=alert_text, inline=False
        )
    else:
        from market_analysis.analyst_runners.macro_runner import (
            build_macro_healthy_status,
        )

        macro_status_text = build_macro_healthy_status(macro_data)
        embed.add_field(
            name="✅ 巨觀狀態",
            value=macro_status_text,
            inline=False,
        )

    embed.set_footer(text="Nexus Seeker | Global Macro Intelligence")
    return embed


def create_fomc_escape_window_embed(
    prob: float,
    direction: str,
    shift_days: int,
    adjusted_start: str,
    adjusted_end: str,
    reason: str,
    is_fallback: bool = False,
    tier_title: str | None = None,
    tactical_directive: str | None = None,
    factors_summary: list[tuple[str, str]] | None = None,
    was_auto_rolled: bool = False,
    original_window_label: str = "",
) -> discord.Embed:
    """建立全維度宏觀流動性逃頂推演矩陣 Embed (繁體中文)"""
    if direction == "前移":
        color = discord.Color.red()  # Tightening risk defense
    elif direction == "後推":
        color = discord.Color.green()  # Liquidity expansion risk-on
    else:
        color = discord.Color.gold()  # Neutral balance

    title_text = "📅 宏觀逃頂：總經流動性撤退推演矩陣 (Macro Escape Matrix)"
    if is_fallback:
        title_text += " [歷史快取/備援]"
    embed = NexusEmbed(title=title_text, color=color)

    # 1. 矩陣狀態
    if tier_title:
        embed.add_field(
            name="🧭 宏觀流動性狀態",
            value=f"**{tier_title}**",
            inline=False,
        )

    # 2. 多因子看板 (ANSI format)
    if factors_summary:
        lines: list[str] = []
        for name, val in factors_summary:
            lines.append(f" ├─ {name}: {val}")
        lines[-1] = lines[-1].replace("├─", "└─")
        panel = "```ansi\n" + "\n".join(lines) + "\n```"
        embed.add_field(
            name="📊 多因子監測結果",
            value=panel,
            inline=False,
        )
    else:
        prob_suffix = " *(歷史快取/備援)*" if is_fallback else ""
        embed.add_field(
            name="📊 利率機率定價 (FedWatch)",
            value=f"下週 FOMC 維持高利率/加息機率：**{prob * 100:.1f}%**{prob_suffix}",
            inline=False,
        )

    # 3. 調整方向與窗口
    shift_label = (
        f"**{direction} {shift_days} 個交易日**"
        if shift_days > 0
        else f"**{direction} (偏移 0 天)**"
    )
    embed.add_field(
        name="🔄 逃頂窗口調整方向",
        value=f"調整方向：{shift_label}",
        inline=True,
    )

    rollover_note = " *(已自動滾動至下季)*" if was_auto_rolled else ""
    embed.add_field(
        name="📆 調整後逃頂窗口預期",
        value=f"預估窗口：**{adjusted_start}** 至 **{adjusted_end}**{rollover_note}",
        inline=True,
    )

    if tactical_directive:
        clean_directive = tactical_directive.replace("**", "")
        directive_lines = [
            "```ansi",
            f" └─ {clean_directive}",
            "```",
        ]
        embed.add_field(
            name="🎯 戰術行動指引",
            value="\n".join(directive_lines),
            inline=False,
        )

    reason_lines = [
        "```ansi",
        f" └─ {reason}",
        "```",
    ]
    embed.add_field(
        name="💡 推演邏輯與風控分析", value="\n".join(reason_lines), inline=False
    )

    embed.set_footer(text="Nexus Risk Engine | 宏觀流動性逃頂推演矩陣")
    return embed
