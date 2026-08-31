"""PowerSqueeze (PSQ) 策略報告 Embed 建構函式。"""

import discord

from datetime import datetime, timezone

from cogs.embed_builders._core import NexusEmbed
from cogs.embed_builders._embed_helpers import add_news_field


def create_psq_embed(data: dict) -> discord.Embed:
    """建構獨立的 PowerSqueeze (PSQ) 戰情報告 Embed"""
    sym = data.get("symbol", "UNKNOWN")
    psq = data.get("psq_result")

    if not psq:  # fallback
        return NexusEmbed(
            title=f"⚡ PowerSqueeze 戰情報告 | {sym}",
            description="無可用數據",
            color=discord.Color.dark_grey(),
        )

    color = discord.Color.purple() if psq.is_squeezing else discord.Color.dark_teal()
    if psq.is_breakout_long:
        color = discord.Color.green()
    elif psq.is_breakout_short:
        color = discord.Color.red()

    embed = NexusEmbed(
        title=f"⚡ PowerSqueeze 戰情報告 | {sym}",
        description=f"💰 最新股價: `${data.get('price', 0.0):.2f}`\n\u200b",
        color=color,
        timestamp=datetime.now(timezone.utc),
    )

    C_RESET = "\u001b[0m"
    C_GREEN = "\u001b[1;32m"
    C_YELLOW = "\u001b[1;33m"
    C_RED = "\u001b[1;31m"

    # Format PSQ quantitative metrics into a premium monospace tree
    psq_lines = ["```ansi"]
    psq_lines.append(f" ⚡ {sym} PowerSqueeze 量化指標")
    psq_lines.append(" ----------------------------------")

    # 1. 能量壓縮
    sq_status = "ON" if psq.is_squeezing else "OFF"
    sq_color = C_RED if psq.is_squeezing else C_GREEN
    psq_lines.append(
        f"  ├─ 能量壓縮狀態: {sq_color}{sq_status}{C_RESET} ({'壓縮蓄力中' if psq.is_squeezing else '無壓縮'})"
    )

    # 2. 動能爆發
    energy_val = (
        "LONG"
        if psq.is_breakout_long
        else ("SHORT" if psq.is_breakout_short else "STABLE")
    )
    energy_color = (
        C_GREEN
        if psq.is_breakout_long
        else (C_RED if psq.is_breakout_short else C_RESET)
    )
    energy_desc = (
        "多頭向上爆發"
        if psq.is_breakout_long
        else ("空頭向下崩潰" if psq.is_breakout_short else "波動蓄勢")
    )
    psq_lines.append(
        f"  ├─ 動能爆發方向: {energy_color}{energy_val}{C_RESET} ({energy_desc})"
    )

    # 3. 線性動能
    mom_trend = "多方主導" if psq.momentum_value > 0 else "空方主導"
    mom_color = C_GREEN if psq.momentum_value > 0 else C_RED
    psq_lines.append(
        f"  ├─ 線性動能 (Mom): {mom_color}{psq.momentum_value:+.2f}{C_RESET} ({mom_trend})"
    )

    # 4. 均線支撐
    dist_val = f"{psq.sma_distance_pct:+.2f}%"
    dist_desc = f"20SMA支撐 (${psq.sma_20:.2f})"
    dist_color = C_GREEN if psq.is_near_support else C_YELLOW
    psq_lines.append(f"  └─ 偏離 20SMA: {dist_color}{dist_val}{C_RESET} ({dist_desc})")

    psq_lines.append("```")
    embed.add_field(name="⚡ PSQ 量化指標", value="\n".join(psq_lines), inline=False)

    # VIX momentum label
    vix_label = (
        psq.vix_momentum_label if hasattr(psq, "vix_momentum_label") else "NORMAL"
    )

    if vix_label != "NORMAL":
        label_map = {
            "OVEREXTENDED_RISK": "⚠️ **過度延伸風險** | 低 VIX 環境多頭訊號可能是牛陷阱",
            "HIGH_CONVICTION_RECOVERY": "🚀 **高確信反彈** | 高 VIX + 空頭減速 = 反轉機會",
        }
        label_text = label_map.get(vix_label, vix_label)
        embed.add_field(name="🏅 VIX 動能判定", value=label_text, inline=False)

    # 最新新聞
    add_news_field(embed, data.get("news_text"))

    # VIX tier info in footer
    vix_spot_val = data.get("vix_spot") or (
        data.get("vix_battle_status", {}).get("vix_spot")
    )
    vix_emoji_val = data.get("vix_battle_status", {}).get("emoji", "")
    vix_name_val = data.get("vix_battle_status", {}).get("name", "")
    vix_footer = (
        f" | VIX: {vix_spot_val:.1f} {vix_emoji_val} {vix_name_val}"
        if vix_spot_val
        else ""
    )
    embed.set_footer(text=f"Nexus Seeker • PowerSqueeze 日K量化引擎{vix_footer}")
    return embed
