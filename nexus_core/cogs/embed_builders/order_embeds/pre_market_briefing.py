"""盤前綜合宏觀與自選股報告 Embed 建構函式。"""

from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

import discord

from cogs.embed_builders._core import NexusEmbed
from cogs.embed_builders._ansi_utils import _pad_string
from cogs.embed_builders._embed_helpers import _safe_embed_field_value


def build_pre_market_briefing_embed(
    macro_data: dict,
    alerts: Optional[List[Any]] = None,
    earnings_alerts: Optional[List[Dict[str, Any]]] = None,
    scanned_symbols: Optional[List[str]] = None,
    warning_days: int = 2,
) -> discord.Embed:
    """建立盤前綜合宏觀與自選股報告 Embed (🌅 盤前綜合宏觀與自選股報告)"""
    has_portfolio_earnings = any(
        item.get("is_portfolio", False) for item in (earnings_alerts or [])
    )

    # 1. 精準計算動態標題後綴與邊框色彩
    if alerts and has_portfolio_earnings:
        base_color = discord.Color.red()
        status_suffix = " [🚨 雙重高危風控警戒]"
    elif alerts:
        base_color = discord.Color.red()
        status_suffix = " [⚠️ 宏觀風控警報觸發]"
    elif has_portfolio_earnings:
        base_color = discord.Color.red()
        status_suffix = " [⚠️ 持倉標的財報高危]"
    elif earnings_alerts:
        base_color = discord.Color.orange()
        status_suffix = " [👀 自選清單財報預警]"
    else:
        base_color = discord.Color.blue()
        status_suffix = " [✅ 總經平穩・無即期財報]"

    embed = NexusEmbed(
        title=f"🌅 報告：盤前綜合宏觀與自選股{status_suffix}",
        color=base_color,
        timestamp=datetime.now(timezone.utc),
    )
    # 嚴格遵循 AGENTS.md：零散 Markdown 清理 (Zero loose markdown noise in body)
    embed.description = None

    # 1. 巨觀數據指標 ANSI 控制台
    dxy = macro_data.get("dxy", 0.0)
    tnx = macro_data.get("tnx", 0.0)
    tnx_change = macro_data.get("tnx_change_bps", 0.0)
    us2y = macro_data.get("us2y", 0.0)
    vix = macro_data.get("vix", 0.0)
    vix_change = macro_data.get("vix_change", 0.0)
    spread = tnx - us2y

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

    lines.append(
        f"{_pad_string('DXY 美元指數', widths[0])} | {_pad_string(f'{dxy:.2f}', widths[1], 'right')} | {_pad_string('-', widths[2], 'right')}"
    )
    lines.append(
        f"{_pad_string('TNX 10Y 公債', widths[0])} | {_pad_string(f'{tnx:.2f}%', widths[1], 'right')} | {_pad_string(f'{tnx_change:+.1f} bps', widths[2], 'right')}"
    )
    spread_note = f"利差 {spread:+.2f}%"
    lines.append(
        f"{_pad_string('US2Y 2Y 公債', widths[0])} | {_pad_string(f'{us2y:.2f}%', widths[1], 'right')} | {_pad_string(spread_note, widths[2], 'right')}"
    )
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

    embed.add_field(
        name="🌍 巨觀數據指標",
        value=_safe_embed_field_value("\n".join(lines), "無數據"),
        inline=False,
    )

    # 2. 宏觀風險警示 / 宏觀狀態 (全 ANSI 包裹)
    if alerts:
        alert_lines = ["```ansi", " ⚠️ 風控警報觸發項目:"]
        for idx, a in enumerate(alerts):
            prefix = "  ├─ " if idx < len(alerts) - 1 else "  └─ "
            alert_lines.append(f"{prefix}\u001b[1;31m{a}\u001b[0m")
        alert_lines.append("```")
        embed.add_field(
            name="🚨 宏觀風險警示 (Macro Alerts)",
            value=_safe_embed_field_value("\n".join(alert_lines), "無警示"),
            inline=False,
        )
    else:
        if vix < 15.0:
            vol_status = f"\u001b[1;32mVIX {vix:.2f} ({vix_change:+.2f})\u001b[0m 低波動沉睡區間，注意權利金較薄"
        elif vix <= 20.0:
            vol_status = f"\u001b[1;32mVIX {vix:.2f} ({vix_change:+.2f})\u001b[0m 常態健康位階，未見異常避險情緒"
        else:
            vol_status = f"\u001b[1;33mVIX {vix:.2f} ({vix_change:+.2f})\u001b[0m 位階偏高但平穩，維持正常戒備"

        bond_status = f"\u001b[1;36m10Y {tnx:.2f}% ({tnx_change:+.1f} bps)\u001b[0m 殖利率平穩 (利差 {spread:+.2f}%)"
        fx_status = f"\u001b[1;37mDXY {dxy:.2f}\u001b[0m 美元走勢溫和，跨國流動性充裕"
        guide_status = (
            "\u001b[1;32m指標全數合規\u001b[0m，維持常態部位與安全邊際"
            if vix < 15.0
            else "\u001b[1;32m指標全數合規\u001b[0m，維持常態部位與標準網格"
        )

        status_lines = [
            "```ansi",
            f" 📈 波動率環境 : {vol_status}",
            f" 🏦 公債與利差 : {bond_status}",
            f" 💵 美元與匯率 : {fx_status}",
            f" 🛡️ 操盤指引   : {guide_status}",
            "```",
        ]
        embed.add_field(
            name="✅ 宏觀狀態",
            value=_safe_embed_field_value("\n".join(status_lines), "正常"),
            inline=False,
        )

    # 3. 自選股財報雷達 (全 ANSI 包裹，依 10 檔一批分欄位)
    if earnings_alerts:
        chunk_size = 10
        chunks = [
            earnings_alerts[i : i + chunk_size]
            for i in range(0, len(earnings_alerts), chunk_size)
        ]
        total_chunks = len(chunks)
        for chunk_idx, chunk in enumerate(chunks, start=1):
            block_lines = ["```ansi"]
            for idx, item in enumerate(chunk):
                sym = item["symbol"]
                e_date = item["earnings_date"]
                days = item["days_left"]
                if item.get("is_portfolio", False):
                    tag = "\u001b[1;31m[持倉高風險]\u001b[0m"
                    sym_styled = f"\u001b[1;31m💎 {sym}\u001b[0m"
                    days_styled = f"\u001b[1;31m倒數 {days} 天\u001b[0m"
                else:
                    tag = "\u001b[0;33m[觀察清單]\u001b[0m"
                    sym_styled = f"\u001b[1;33m👀 {sym}\u001b[0m"
                    days_styled = f"\u001b[1;33m倒數 {days} 天\u001b[0m"

                block_lines.append(f" {sym_styled} {tag}")
                block_lines.append(f"  └─ 📅 財報日: {e_date} │ {days_styled}")
                if idx < len(chunk) - 1:
                    block_lines.append("")
            block_lines.append("```")

            field_name = "🚨 自選股財報季雷達預警 (Earnings Radar)"
            if total_chunks > 1:
                field_name += f" (第 {chunk_idx}/{total_chunks} 批)"
            embed.add_field(
                name=field_name,
                value=_safe_embed_field_value("\n".join(block_lines), "無預警"),
                inline=False,
            )
    else:
        scanned_list = ", ".join(scanned_symbols) if scanned_symbols else "無"
        safe_lines = [
            "```ansi",
            f" 🎯 已掃描標的 : {scanned_list}",
            f" 🛡️ 風控評定   : \u001b[1;32m近 {warning_days} 日內無財報發布風險，安全過關！\u001b[0m",
            "```",
        ]
        embed.add_field(
            name="✅ 自選股財報季雷達",
            value=_safe_embed_field_value("\n".join(safe_lines), "安全過關"),
            inline=False,
        )

    embed.set_footer(text="🌌 Nexus Seeker • 盤前綜合簡報")
    return embed
