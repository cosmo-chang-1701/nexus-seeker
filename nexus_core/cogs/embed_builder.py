"""Centralized Discord embed builders for Nexus Seeker.

Cog and service layers should assemble data and route notifications, while this
module owns the final Discord embed presentation. Helpers are grouped with
lightweight section markers so future output work stays centralized and easier
to navigate.
"""

import discord
import logging
import psutil
import re

from datetime import datetime, timezone
from typing import List, Dict, Any, Optional

from models.schemas import WatchlistOptionPlan
from ui import panel_renderer

logger = logging.getLogger(__name__)


# ============================================================================
# Visual Consistency Embed Subclass (Refactoring Embed Page Layout)
# ============================================================================

_OriginalEmbed = discord.Embed


class NexusEmbed(discord.Embed):
    """自訂 Embed 子類別，用以動態實現一致的版面設計、精緻調色盤與標準 Footer 排版。"""

    def __init__(self, *args, **kwargs):
        # 1. 統一對齊和諧且精美的高級調色盤 (Curated Aesthetic Palette)
        color = kwargs.get("color")
        if color is not None:
            if color == discord.Color.blue():
                kwargs["color"] = discord.Color(0x3498DB)
            elif color == discord.Color.red() or color == discord.Color.dark_red():
                kwargs["color"] = discord.Color(0xE74C3C)
            elif color == discord.Color.green():
                kwargs["color"] = discord.Color(0x2ECC71)
            elif color == discord.Color.orange():
                kwargs["color"] = discord.Color(0xF39C12)
            elif color == discord.Color.blurple():
                kwargs["color"] = discord.Color(0x5865F2)
        else:
            kwargs["color"] = discord.Color(0x3498DB)

        super().__init__(*args, **kwargs)

        # 2. 確保時間戳記一致存在
        if self.timestamp is None:
            self.timestamp = datetime.now(timezone.utc)

    @property
    def color(self):
        return super().color

    @color.setter
    def color(self, value):
        if value is not None:
            if value == discord.Color.blue():
                value = discord.Color(0x3498DB)
            elif value == discord.Color.red() or value == discord.Color.dark_red():
                value = discord.Color(0xE74C3C)
            elif value == discord.Color.green():
                value = discord.Color(0x2ECC71)
            elif value == discord.Color.orange():
                value = discord.Color(0xF39C12)
            elif value == discord.Color.blurple():
                value = discord.Color(0x5865F2)
        _OriginalEmbed.color.fset(self, value)

    @property
    def colour(self):
        return self.color

    @colour.setter
    def colour(self, value):
        self.color = value

    def set_footer(self, *, text: str = None, icon_url: str = None):
        if text:
            # 3. 統一版面 Footer 排版 signature
            prefix = "🌌 Nexus Seeker • "
            clean_text = text
            for p in (
                "🌌 Nexus Seeker • ",
                "Nexus Seeker • ",
                "Nexus Seeker | ",
                "Nexus Seeker ",
            ):
                if clean_text.startswith(p):
                    clean_text = clean_text[len(p) :]
            text = f"{prefix}{clean_text}"
        super().set_footer(text=text, icon_url=icon_url)

    @classmethod
    def from_dict(cls, data):
        embed = _OriginalEmbed.from_dict(data)
        nexus_embed = cls(
            title=embed.title,
            description=embed.description,
            color=embed.color,
            timestamp=embed.timestamp,
            url=embed.url,
        )
        if embed.footer:
            nexus_embed.set_footer(
                text=embed.footer.text, icon_url=embed.footer.icon_url
            )
        if embed.image:
            nexus_embed.set_image(url=embed.image.url)
        if embed.thumbnail:
            nexus_embed.set_thumbnail(url=embed.thumbnail.url)
        if embed.author:
            nexus_embed.set_author(
                name=embed.author.name,
                url=embed.author.url,
                icon_url=embed.author.icon_url,
            )
        for field in embed.fields:
            nexus_embed.add_field(
                name=field.name, value=field.value, inline=field.inline
            )
        return nexus_embed


# 置換當前 module 內部的 discord.Embed 參照，完美攔截並重構所有 Embed 版面
discord.Embed = NexusEmbed  # type: ignore[misc]


# ============================================================================
# Visual-width and content-safety utilities
# ============================================================================


def _visual_len(s: str) -> int:
    """計算字串的視覺寬度，中文字元與中文標點視為雙倍寬度。"""
    return sum(
        2
        if (ord(c) > 127 or 0x3000 <= ord(c) <= 0x303F or 0xFF00 <= ord(c) <= 0xFFEF)
        else 1
        for c in s
    )


def _pad_string(s: str, width: int, align: str = "left") -> str:
    """根據視覺寬度對字串進行填充。"""
    vlen = _visual_len(s)
    pad_len = max(0, width - vlen)
    if align == "right":
        return " " * pad_len + s
    elif align == "center":
        left_pad = pad_len // 2
        right_pad = pad_len - left_pad
        return " " * left_pad + s + " " * right_pad
    else:
        return s + " " * pad_len


def _visual_truncate(s: str, max_vlen: int) -> str:
    """根據視覺寬度截斷字串，避免中文字元被切成一半。"""
    return panel_renderer.visual_truncate(s, max_vlen)


def _wrap_visual(text: str, width: int, indent: str = "") -> list[str]:
    return panel_renderer.wrap_visual(text, width, indent)


def _parse_and_format_positions_table(
    positions_list: List[str], survival_runway=None
) -> str:
    if not positions_list:
        return "目前無持倉部位。"

    table_lines = ["```ansi"]
    headers = [
        "ID",
        "標的",
        "方向",
        "類型",
        "到期日 (DTE)",
        "履約價",
        "數",
        "建立權利金",
        "盤中現價",
        "當前損益 (PnL)",
        "隱含波動率 (IV/IVR)",
        "戰略中心執行路由",
    ]
    widths = [2, 5, 4, 4, 15, 7, 2, 10, 8, 22, 18, 16]

    header_line = " | ".join(
        _pad_string(h, w, "left" if i in (0, 1, 2, 3, 4, 11) else "right")
        for i, (h, w) in enumerate(zip(headers, widths))
    )
    table_lines.append(header_line)
    table_lines.append("-" * (sum(widths) + 3 * (len(widths) - 1)))

    total_debit_cost = 0.0
    total_credit_cash = 0.0
    total_unrealized_pnl = 0.0

    for idx, pos_item in enumerate(positions_list):
        # Regex parsing
        sym_match = re.search(r"🔹\s*\*\*(.*?)\*\*", pos_item)
        exp_match = re.search(r"`(\d{4}-\d{2}-\d{2})`", pos_item)
        strike_type_match = re.search(r"`?\$([\d\.]+)`?\s*\*\*(.*?)\*\*", pos_item)

        cost_match = re.search(r"成本:\s*`\$?([\d\.,\-]+)`", pos_item)
        price_match = re.search(r"現價:\s*`\$?([\d\.,\-]+)`", pos_item)
        dte_match = re.search(r"DTE:\s*`(.*?)`", pos_item)
        status_match = re.search(r"動作:\s*(.*)", pos_item)

        dir_match = re.search(r"方向:\s*`(.*?)`", pos_item)
        qty_match = re.search(r"數量:\s*`(.*?)`", pos_item)
        iv_match = re.search(r"IV/IVR:\s*`(.*?)`", pos_item)

        symbol = sym_match.group(1).strip() if sym_match else "UNKNOWN"
        expiry = exp_match.group(1).strip() if exp_match else "N/A"
        strike_val = strike_type_match.group(1).strip() if strike_type_match else ""
        opt_type = strike_type_match.group(2).strip() if strike_type_match else ""
        type_letter = (
            "CALL"
            if "CALL" in opt_type.upper()
            else "PUT"
            if "PUT" in opt_type.upper()
            else opt_type
        )

        direction = dir_match.group(1).strip() if dir_match else "BTO"
        qty = abs(float(qty_match.group(1).strip())) if qty_match else 1.0

        # 建立權利金與現價
        cost_val = (
            float(cost_match.group(1).strip().replace(",", "")) if cost_match else 0.0
        )
        price_val = (
            float(price_match.group(1).strip().replace(",", "")) if price_match else 0.0
        )

        # 計算損益
        if direction == "BTO":
            debit_cost = cost_val * 100 * qty
            total_debit_cost += debit_cost
            pnl_val = (price_val - cost_val) * 100 * qty
        else:
            credit_cash = cost_val * 100 * qty
            total_credit_cash += credit_cash
            pnl_val = (cost_val - price_val) * 100 * qty

        total_unrealized_pnl += pnl_val

        # 到期日與 DTE
        dte_val = dte_match.group(1).strip() if dte_match else "0"
        if "天" in dte_val:
            dte_val = dte_val.replace("天", "").strip()
        dte_str = f"{expiry} ({dte_val}d)"

        # 履約價
        strike_str = f"${strike_val}"

        # 數量
        qty_str = f"{int(qty)}" if qty.is_integer() else f"{qty}"

        # 建立權利金 / 盤中現價
        cost_str = f"${cost_val:.2f}"
        price_str = f"${price_val:.2f}"

        # 當前損益 (PnL)
        pnl_pct = 0.0
        if cost_val > 0.0:
            if direction == "BTO":
                pnl_pct = (price_val - cost_val) / cost_val
            else:
                pnl_pct = (cost_val - price_val) / cost_val

        pnl_text = f"{pnl_val:+.2f} ({pnl_pct*100:+.2f}%)"
        if direction == "STO":
            pnl_text += " [C]"

        # ANSI 著色處理
        pnl_text_truncated = _visual_truncate(pnl_text, widths[9])
        if pnl_val > 0:
            pnl_fmt = f" [0;32m{pnl_text_truncated} [0m"
        elif pnl_val < 0:
            pnl_fmt = f" [0;31m{pnl_text_truncated} [0m"
        else:
            pnl_fmt = pnl_text_truncated

        # IV / IVR
        iv_ivr_str = iv_match.group(1).strip() if iv_match else "--% / --%"

        # 戰略中心執行路由 (動作)
        status = status_match.group(1).strip() if status_match else "HOLD"

        symbol = _visual_truncate(symbol, widths[1])
        direction = _visual_truncate(direction, widths[2])
        type_letter = _visual_truncate(type_letter, widths[3])
        dte_str = _visual_truncate(dte_str, widths[4])
        strike_str = _visual_truncate(strike_str, widths[5])
        qty_str = _visual_truncate(qty_str, widths[6])
        cost_str = _visual_truncate(cost_str, widths[7])
        price_str = _visual_truncate(price_str, widths[8])
        iv_ivr_str = _visual_truncate(iv_ivr_str, widths[10])
        status = _visual_truncate(status, widths[11])

        id_str = f"{idx+1:02d}"

        cols = [
            _pad_string(id_str, widths[0], "left"),
            _pad_string(symbol, widths[1], "left"),
            _pad_string(direction, widths[2], "left"),
            _pad_string(type_letter, widths[3], "left"),
            _pad_string(dte_str, widths[4], "left"),
            _pad_string(strike_str, widths[5], "right"),
            _pad_string(qty_str, widths[6], "right"),
            _pad_string(cost_str, widths[7], "right"),
            _pad_string(price_str, widths[8], "right"),
            _pad_string(pnl_text_truncated, widths[9], "right").replace(
                pnl_text_truncated, pnl_fmt
            ),
            _pad_string(iv_ivr_str, widths[10], "right"),
            _pad_string(status, widths[11], "left"),
        ]
        table_lines.append(" | ".join(cols))

    table_lines.append("")
    table_lines.append("財務摘要 (Financial Summary)")
    table_lines.append(
        f" ├─ 衍生品實質現金暴露 (Debit Cost)   : ${total_debit_cost:,.2f} USD"
    )
    table_lines.append(
        f" ├─ 造市商已沒收權利金 (Credit Cash)   : ${total_credit_cash:,.2f} USD"
    )

    pnl_sign = "+" if total_unrealized_pnl > 0 else ""
    pnl_color_start = (
        " [0;32m"
        if total_unrealized_pnl > 0
        else " [0;31m"
        if total_unrealized_pnl < 0
        else ""
    )
    pnl_color_end = " [0m" if total_unrealized_pnl != 0 else ""
    table_lines.append(
        f" ├─ 盤中實時未實現損益 (Unrealized PnL): {pnl_color_start}{pnl_sign}${total_unrealized_pnl:,.2f} USD{pnl_color_end}"
    )

    if survival_runway is not None:
        if survival_runway >= 9999:
            runway_years_str = "無限 年 (鐵血不破)"
        else:
            runway_years_str = f"{float(survival_runway)/365.0:.1f}+ 年 (鐵血不破)"
    else:
        runway_years_str = "4.6+ 年 (鐵血不破)"
    table_lines.append(f" └─ 全域生存跑道安全係數 (Runway Buffer): {runway_years_str}")

    table_lines.append("```")
    return "\n".join(table_lines)


def _is_macro_report_marker(line: str) -> bool:
    """較穩健地辨識宏觀風險段落起始行。"""
    if not line:
        return False
    normalized = line.strip()
    if not normalized.startswith("🌐"):
        return False
    return ("宏觀風險" in normalized) or ("資金水位報告" in normalized)


def _truncate_with_boundary(text: str, max_len: int) -> str:
    """優先在換行或句點邊界截斷，避免硬切造成可讀性差。"""
    return panel_renderer.truncate_with_boundary(text, max_len)


def _safe_embed_field_value(text: str, fallback: str, max_len: int = 1024) -> str:
    """確保欄位值非空且符合 Discord 長度上限。"""
    value = (text or "").strip()
    if not value:
        value = fallback

    # 保留尾端間距，讓下一個欄位視覺更乾淨。
    suffix = "\n\u200b"
    room = max(1, max_len - len(suffix))
    value = _truncate_with_boundary(value, room)
    value = value + suffix

    if len(value) > max_len:
        value = _truncate_with_boundary(value, max_len)
    if not value.strip():
        value = fallback + suffix
    return value


def _chunk_ansi_table(
    header: str, divider: str, data_lines: List[str], max_len: int = 1024
) -> List[str]:
    """將 ANSI 表格切成多個符合 Discord Embed field value 長度限制的區塊。"""
    return panel_renderer.chunk_ansi_table(
        header,
        divider,
        data_lines,
        max_len=max_len,
    )


def _safe_float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _report_embed_color(text: str) -> discord.Color:
    """根據報告內容關鍵字推導一致的報告顏色。"""
    if "🚨" in text or "🆘" in text:
        return discord.Color.red()
    if "⚠️" in text:
        return discord.Color.orange()
    return discord.Color.blue()


def _extract_report_batch(report_type: str) -> str:
    match = re.search(r"(\[[^\]]+\])", report_type)
    if match:
        return match.group(1)
    return report_type


def _parse_ai_report_sections(ai_report_text: str) -> list[tuple[str, str]]:
    """將 LLM 報告切成可直接映射到 Embed 欄位的段落。"""
    sections = re.split(r"\n(?=\d+\.\s+\*\*|(?:\*\*[\u2600-\u2BFF]))", ai_report_text)
    parsed_sections: list[tuple[str, str]] = []

    if len(sections) <= 1:
        return parsed_sections

    for section in sections:
        section = section.strip()
        if not section:
            continue

        lines = section.split("\n", 1)
        header = lines[0].replace("**", "").strip()
        header = re.sub(r"^\d+\.\s*", "", header)
        content = lines[1].strip() if len(lines) > 1 else "無內容"
        content = re.sub(r"^-{3,}$", "", content, flags=re.MULTILINE).strip()
        if header:
            parsed_sections.append((header, content or "無詳細資訊"))

    return parsed_sections


def _safe_embed_codeblock_value(
    content: str,
    fallback: str,
    *,
    lang: str = "text",
    max_len: int = 1024,
) -> str:
    """產生安全的 code block 欄位值，避免截斷導致 fence 不閉合。"""
    return panel_renderer.safe_codeblock_value(
        content,
        fallback,
        lang=lang,
        max_len=max_len,
        add_zws_suffix=True,
    )


def _build_watchlist_style_panel(
    title_line: str,
    content: str,
    *,
    width: int = 45,
    empty_msg: str = "暫無資料",
) -> str:
    """建立 Watchlist 半小時戰報風格的 ANSI 面板文字內容 (不含尾端 \u200b)。"""
    return panel_renderer.build_watchlist_style_panel(
        title_line,
        content,
        width=width,
        empty_msg=empty_msg,
    )


def _append_ai_report_fields(
    embed: discord.Embed,
    ai_report_text: str,
    *,
    fallback_field_name: str = "🤖 AI 分析摘要",
) -> None:
    """將 AI 報告以一致的欄位格式附加到 Embed。"""
    sections = _parse_ai_report_sections(ai_report_text)
    if sections:
        for header, content in sections:
            embed.add_field(
                name=header,
                value=_safe_embed_field_value(content, "無詳細資訊"),
                inline=False,
            )
        return

    embed.add_field(
        name=fallback_field_name,
        value=_safe_embed_field_value(ai_report_text, "無詳細資訊"),
        inline=False,
    )


def split_embed_by_fields(
    embed: discord.Embed, max_size: int = 5000
) -> list[discord.Embed]:
    """將多欄位報告拆成多則 Embed，儘量合併欄位以防訊息過於零碎，且確保單則長度不超過 max_size。"""
    if len(embed.fields) <= 1:
        return [embed]

    base_payload = embed.to_dict()
    base_payload.pop("fields", None)

    def _get_base_len(include_desc: bool) -> int:
        total = 0
        title = base_payload.get("title")
        if title:
            total += len(title)
        if include_desc:
            desc = base_payload.get("description")
            if desc:
                total += len(desc)
        footer = base_payload.get("footer")
        if footer and footer.get("text"):
            total += len(footer["text"])
        author = base_payload.get("author")
        if author and author.get("name"):
            total += len(author["name"])
        return total

    base_len_first = _get_base_len(include_desc=True)
    base_len_subsequent = _get_base_len(include_desc=False)

    groups: List[List[Any]] = []
    current_group: List[Any] = []
    current_len = 0

    for field in embed.fields:
        field_len = len(field.name or "") + len(field.value or "")
        is_first = len(groups) == 0
        base_len = base_len_first if is_first else base_len_subsequent

        if current_group and (
            base_len + current_len + field_len > max_size or len(current_group) >= 24
        ):
            groups.append(current_group)
            current_group = [field]
            current_len = field_len
        else:
            current_group.append(field)
            current_len += field_len

    if current_group:
        groups.append(current_group)

    if len(groups) <= 1:
        return [embed]

    split_embeds: list[discord.Embed] = []
    total = len(groups)

    for index, group in enumerate(groups, start=1):
        payload = dict(base_payload)
        title = payload.get("title")
        if title:
            payload["title"] = f"{title} ({index}/{total})"
        if index > 1:
            payload.pop("description", None)

        split_embed = discord.Embed.from_dict(payload)
        for field in group:
            split_embed.add_field(
                name=field.name, value=field.value, inline=field.inline
            )
        split_embeds.append(split_embed)

    return split_embeds


# ============================================================================
# Shared field-formatting helpers
# ============================================================================


def add_news_field(embed, news_text):
    if news_text:
        if len(news_text) > 1000:
            news_text = news_text[:997] + "..."
        news_context = f"```{news_text}\n\u200b```"
        embed.add_field(name="📰 最新新聞", value=news_context, inline=False)


def add_reddit_field(embed, reddit_text):
    if reddit_text:
        if len(reddit_text) > 1000:
            reddit_text = reddit_text[:997] + "..."
        reddit_context = f"```{reddit_text}\n\u200b```"
        embed.add_field(name="📰 Reddit 討論", value=reddit_context, inline=False)


def _build_embed_base(data, strategy, stock_cost):
    colors = {
        "STO_PUT": discord.Color.green(),
        "STO_CALL": discord.Color.red(),
        "BTO_CALL": discord.Color.blue(),
        "BTO_PUT": discord.Color.orange(),
    }
    titles = {
        "STO_PUT": "🟢 賣出賣權 (STO Put)",
        "STO_CALL": "🔴 賣出買權 (STO Call)",
        "BTO_CALL": "🚀 買入買權 (BTO Call)",
        "BTO_PUT": "⚠️ 買入賣權 (BTO Put)",
    }

    is_covered = strategy == "STO_CALL" and stock_cost > 0.0
    if is_covered:
        titles["STO_CALL"] = "🛡️ 掩護性買權 (Covered Call)"
        colors["STO_CALL"] = discord.Color.teal()

    embed = discord.Embed(
        title=f"{titles.get(strategy, strategy)} | {data.get('symbol', 'UNKNOWN')}",
        description=f"📅 **到期日:** `{data.get('target_date', 'UNKNOWN')}` ｜ 🎯 **履約價:** `${data.get('strike', 'UNKNOWN')}`\n\u200b",
        color=colors.get(strategy, discord.Color.default()),
    )
    return embed, is_covered


def _add_vix_battle_status_field(embed, data):
    """Add VIX Battle Ladder status indicator (highest priority field)."""
    vix_status = data.get("vix_battle_status") or {}
    vix_spot = vix_status.get("vix_spot") or data.get("vix_spot")
    tier_name = vix_status.get("name") or data.get("vix_tier_name", "N/A")
    tier_emoji = vix_status.get("emoji") or data.get("vix_tier_emoji", "")
    delta_cap = vix_status.get("sto_delta_cap") or data.get("vix_sto_delta_cap", 0.0)
    sizing_mult = vix_status.get("sizing_multiplier") or data.get(
        "vix_sizing_multiplier", 1.0
    )

    if vix_spot is None:
        return

    status_line = f"{tier_emoji} **{tier_name}** | VIX: `{vix_spot:.1f}`"
    details = []
    if delta_cap != 0.0:
        details.append(f"Delta Cap: `{delta_cap:.2f}`")
    if sizing_mult != 1.0:
        details.append(f"\u5009\u4f4d\u4e58\u6578: `{sizing_mult:.1f}x`")

    value = status_line
    if details:
        value += "\n" + " | ".join(details)
    value += "\n\u200b"

    embed.add_field(name="🛡️ VIX 戰情階梯狀態", value=value, inline=False)


def _add_market_overview_fields(embed, data):
    beta = data.get("beta", 1.0)
    beta_status = "🚀" if beta > 1.3 else ("⚖️" if beta >= 0.8 else "🧊")
    embed.add_field(
        name="🏷️ 標價 / Beta\u2800\u2800",
        value=f"${data['price']:.2f} / `{beta:.2f}` {beta_status}\n\u200b",
        inline=True,
    )
    embed.add_field(
        name="📈 RSI / 20MA\u2800\u2800\u2800",
        value=f"{data['rsi']:.2f} / ${data['sma20']:.2f}\n\u200b",
        inline=True,
    )
    hvr_status = (
        "🔥 高"
        if data["hv_rank"] >= 50
        else ("⚡ 中" if data["hv_rank"] >= 30 else "🧊 低")
    )
    embed.add_field(
        name="🔥 HV Rank\u2800\u2800\u2800\u2800",
        value=f"`{data['hv_rank']:.1f}%` {hvr_status}\n\u200b",
        inline=True,
    )


def _add_volatility_fields(embed, data, strategy):
    vrp_pct = data.get("vrp", 0.0) * 100
    vrp_icon = (
        "✅"
        if (("STO" in strategy and vrp_pct > 0) or ("BTO" in strategy and vrp_pct < 0))
        else "⚠️"
    )
    embed.add_field(
        name="⚖️ VRP 溢酬\u2800\u2800\u2800\u2800",
        value=f"`{vrp_pct:+.2f}%` {vrp_icon}\n\u200b",
        inline=True,
    )
    ts_ratio_str = f"`{data['ts_ratio']:.2f}` {data['ts_state']}"
    embed.add_field(
        name="⏳ 個股 IV 期限結構\u2800\u2800",
        value=f"{ts_ratio_str}\n\u200b",
        inline=True,
    )
    v_skew_str = f"`{data['v_skew']:.2f}` {data.get('v_skew_state', '')}"
    embed.add_field(
        name="📉 垂直偏態\u2800\u2800\u2800\u2800",
        value=f"{v_skew_str}\n\u200b",
        inline=True,
    )

    # ---------------- VIX 306 Volatility Metrics ----------------
    vts_str = f"`{data.get('vix_vts_ratio', 1.0):.2f}` {data.get('vix_regime', '')}"
    embed.add_field(
        name="🌐 大盤 VIX 期限結構\u2800", value=f"{vts_str}\n\u200b", inline=True
    )

    z30 = data.get("vix_z30", 0.0)
    z60 = data.get("vix_z60", 0.0)
    z_icon = "📈 擴張" if z30 > 0.5 and z60 > 0 else "📉 收斂"
    embed.add_field(
        name="🔥 VIX 30/60 Z-Score",
        value=f"Z30:`{z30:.1f}` Z60:`{z60:.1f}` {z_icon}\n\u200b",
        inline=True,
    )

    tail_risk = (
        "🚨 觸發降規 (1/4 Kelly)"
        if data.get("is_high_tail_risk", False)
        else "✅ 正常 (1/2 Kelly)"
    )
    embed.add_field(
        name="🛡️ 尾部風險管理\u2800\u2800\u2800",
        value=f"{tail_risk}\n\u200b",
        inline=True,
    )


def _add_performance_and_kelly_fields(embed, data, user_capital):
    """添加績效與風控（含凱利倉位計算）欄位，並校正部位方向"""
    strategy = data.get("strategy", "")
    raw_delta = data.get("delta", 0.0)
    weighted_delta = data.get("weighted_delta", 0.0)

    # 🚀 方向校正邏輯：
    # 若是賣方 (STO)，部位方向 = 合約方向 * -1 (賣出負 Delta 是看多，賣出正 Delta 是看空)
    # 若是買方 (BTO)，部位方向 = 合約方向 (買入什麼就是什麼)
    pos_multiplier = -1 if "STO" in strategy else 1
    pos_weighted_shares = weighted_delta * pos_multiplier

    # 1. 希臘字母與部位方向
    embed.add_field(
        name="🧩 Delta (部位加權)\u2800\u2800",
        value=f"{raw_delta:.3f} (`{pos_weighted_shares:+.1f}`股)\n\u200b",
        inline=True,
    )

    # 2. 獲利效率與隱含波動率
    embed.add_field(
        name="💰 AROC / IV\u2800\u2800\u2800\u2800",
        value=f"`{data['aroc']:.1f}%` / {data['iv']:.1%}\n\u200b",
        inline=True,
    )

    # 3. 凱利建議邏輯
    alloc_pct = data.get("alloc_pct", 0.0)
    suggested = data.get("suggested_contracts", 0)

    if alloc_pct <= 0:
        kelly_value = "`不建議建倉`"
    elif not user_capital or user_capital <= 0:
        kelly_value = f"`未設資金` ({alloc_pct*100:.1f}%)"
    else:
        # 使用與主邏輯同步的 25% Kelly 上限顯示
        kelly_value = (
            f"`{suggested} 口` ({min(alloc_pct, 0.25)*100:.1f}%)"
            if suggested > 0
            else "`本金不足`"
        )

    embed.add_field(
        name="🧮 凱利原始建議\u2800\u2800", value=f"{kelly_value}\n\u200b", inline=True
    )


def _add_earnings_fields(embed, data, strategy):
    """添加財報預期波動欄位"""
    if 0 <= data.get("earnings_days", -1) <= 14:
        mmm_str = f"±{data['mmm_pct']:.1f}% (倒數 {data['earnings_days']} 天)"
        bounds_str = f"🛡️ 安全區間: **`${data['safe_lower']:.2f}`** ~ **`${data['safe_upper']:.2f}`**"
        strike = data["strike"]

        if "STO" in strategy:
            is_safe = (strategy == "STO_PUT" and strike <= data["safe_lower"]) or (
                strategy == "STO_CALL" and strike >= data["safe_upper"]
            )
            safety_icon = (
                "✅ 避開雷區 (適宜收租)" if is_safe else "💣 位於雷區 (極高風險)"
            )
        else:
            safety_icon = "🎲 財報盲盒 (注意 IV Crush 波動率壓縮風險)"

        embed.add_field(
            name="📊 財報預期波動 (MMM)",
            value=f"`{mmm_str}`\n{bounds_str}\n{safety_icon}\n\u200b",
            inline=False,
        )


def _add_covered_call_fields(embed, data, stock_cost):
    """添加 Covered Call 專屬防護欄位"""
    bid = data.get("bid", 0)
    true_breakeven = stock_cost - bid
    yoc = (bid / stock_cost) * 100 if stock_cost > 0 else 0

    cc_info = (
        f"📦 **真實現股成本:** `${stock_cost:.2f}`\n"
        f"🛡️ **真實下檔防線:** `${true_breakeven:.2f}`\n"
        f"💸 **單次收租殖利率 (Yield on Cost):** `{yoc:.2f}%`\n"
        f"👉 *您的持倉成本已透過收租進一步降低！*\n\u200b"
    )
    embed.add_field(name="🛡️ Covered Call 專屬防護", value=cc_info, inline=False)


def _add_expected_move_fields(embed, data, strategy, is_covered):
    """添加預期波動區間與損益兩平防線欄位"""
    em = data.get("expected_move", 0.0)
    em_lower = data.get("em_lower", 0.0)
    em_upper = data.get("em_upper", 0.0)

    if "STO_PUT" in strategy:
        breakeven = data["strike"] - data.get("bid", 0)
        safe = breakeven < em_lower
        safety_text = (
            "✅ 防線已建構於預期暴跌區間外"
            if safe
            else "⚠️ 損益兩平點位於預期波動區間內，風險較高"
        )
        em_info = f"1σ 預期下緣: `${em_lower:.2f}` (預期最大跌幅 -${em:.2f})\n🛡️ 損益兩平點: **`${breakeven:.2f}`**\n{safety_text}\n\u200b"
        embed.add_field(name="🎯 機率圓錐 (1σ 預期波動)", value=em_info, inline=False)

    elif "STO_CALL" in strategy:
        breakeven = data["strike"] + data.get("bid", 0)
        safe = breakeven > em_upper
        if is_covered:
            safety_text = "✅ 若漲破此價位，將以最高獲利出場 (股票被 Call 走)"
        else:
            safety_text = (
                "✅ 防線已建構於預期暴漲區聯外"
                if safe
                else "⚠️ 損益兩平點位於預期波動區間內，風險較高"
            )

        em_info = f"1σ 預期上緣: `${em_upper:.2f}` (預期最大漲幅 +${em:.2f})\n🛡️ 合約兩平點: **`${breakeven:.2f}`**\n{safety_text}\n\u200b"
        embed.add_field(name="🎯 機率圓錐 (1σ 預期波動)", value=em_info, inline=False)

    elif "BTO_PUT" in strategy:
        breakeven = data["strike"] - data.get("ask", 0)
        em_info = f"1σ 預期下緣: `${em_lower:.2f}` (預期最大跌幅 -${em:.2f})\n🛡️ 損益兩平點: **`${breakeven:.2f}`**\n✅ 目標跌破此防線即開始獲利\n\u200b"
        embed.add_field(name="🎯 機率圓錐 (1σ 預期波動)", value=em_info, inline=False)

    elif "BTO_CALL" in strategy:
        breakeven = data["strike"] + data.get("ask", 0)
        em_info = f"1σ 預期上緣: `${em_upper:.2f}` (預期最大漲幅 +${em:.2f})\n🛡️ 損益兩平點: **`${breakeven:.2f}`**\n✅ 目標突破此防線即開始獲利\n\u200b"
        embed.add_field(name="🎯 機率圓錐 (1σ 預期波動)", value=em_info, inline=False)


def _add_liquidity_fields(embed, data):
    """添加報價與流動性分析欄位"""
    mid_price = data.get("mid_price", (data.get("bid", 0) + data.get("ask", 0)) / 2)
    liq_status = data.get("liq_status", "N/A")
    liq_msg = data.get("liq_msg", "")

    spread_info = (
        f"**Bid:** `{data.get('bid', 0):.2f}` ｜ **Ask:** `{data.get('ask', 0):.2f}` (價差 `{data.get('spread_ratio', 0):.1f}%`)\n"
        f"**狀態:** {liq_status} {liq_msg}\n"
        f"🎯 **Limit (中價掛單建議):** `{mid_price:.2f}`\n\u200b"
    )
    embed.add_field(name="💱 報價與流動性分析", value=spread_info, inline=False)


def _add_strategy_upgrade_fields(embed, data, strategy):
    """添加策略升級提示欄位"""
    if strategy in ["BTO_CALL", "BTO_PUT"]:
        hedge_strike = data.get("suggested_hedge_strike")
        if hedge_strike:
            spread_type = (
                "多頭價差 (Bull Call Spread)"
                if strategy == "BTO_CALL"
                else "空頭價差 (Bear Put Spread)"
            )
            hedge_type = "Call" if strategy == "BTO_CALL" else "Put"

            upgrade_text = (
                f"為抵銷 Theta (時間價值) 衰減並降低建倉成本，\n"
                f"建議在買入本合約的同時，賣出更價外的 **${hedge_strike:.0f} {hedge_type}**\n"
                f"👉 組合為: **{spread_type}**\n\u200b"
            )
            embed.add_field(
                name="💡 經理人策略升級建議", value=upgrade_text, inline=False
            )


def _add_risk_optimization_fields(embed, data, user_capital=None):
    """
    添加事前曝險模擬與自動風控優化建議
    🚀 強化版：增加閾值動態化與基準價校驗
    """
    projected_pct = data.get("projected_exposure_pct")
    # 若無數據則不顯示 (注意：不要用 if projected_pct == 0)
    if projected_pct is None:
        return

    safe_qty = data.get("safe_qty", 0)
    hedge_spy = data.get("hedge_spy", 0.0)
    suggested = data.get("suggested_contracts", 0)

    # 🚀 修正點 1：風險閾值應從數據中取得，或設為全局變數
    # 避免後台改了 10% 這裡還在顯示 15%
    RISK_THRESHOLD = data.get("risk_limit", 15.0)

    # 1. 曝險現況區塊
    is_overloaded = abs(projected_pct) > RISK_THRESHOLD

    if is_overloaded:
        sim_status = "🚨 警告：曝險過載"
        # 使用 diff 語法渲染紅色背景
        sim_block = (
            f"```diff\n"
            f"- 成交後預期總曝險: {projected_pct:+.1f}%\n"
            f"- 超過 {RISK_THRESHOLD}% 宏觀紅線\n"
            f"```"
        )
    else:
        sim_status = "✅ 狀態：風險受控"
        # 使用 yaml 語法渲染綠色背景
        sim_block = (
            f"```yaml\n"
            f"成交後的預期總曝險: {projected_pct:+.1f}%\n"
            f"符合資產組合平衡標準\n"
            f"```"
        )

    embed.add_field(
        name=f"🛡️ What-if 曝險模擬 | {sim_status}",
        value=f"{sim_block}\n\u200b",
        inline=False,
    )

    # 2. Nexus Risk Optimizer 自動優化建議
    if suggested > safe_qty:
        opt_title = "⚖️ Nexus Risk Optimizer (自動優化建議)"

        # 🚀 修正點 2：加入基準 SPY 價格的動態提示 (讓對沖建議更可信)
        spy_p = data.get("spy_price", 690.0)

        actions = ["--- 偵測到風險超標，執行自動降規 ---"]
        actions.append(f"❌ 原始建議: {suggested} 口")
        actions.append(f"✅ 安全成交: {safe_qty} 口 (符合風控)")

        if safe_qty == 0 and hedge_spy != 0:
            actions.append("\n⚠️ 警告: 即使下 1 口也過載")
            direction = "賣出" if hedge_spy > 0 else "買入"
            # 格式化對沖股數，避免出現 22.2222222
            actions.append(
                f"🛡️ 建議對沖: {direction} {abs(hedge_spy):.1f} 股 SPY (@${spy_p:.1f})"
            )

        opt_block = "```diff\n" + "\n".join(actions) + "\n```"
        embed.add_field(name=opt_title, value=f"{opt_block}\n\u200b", inline=False)


def _add_hedge_unlock_fields(embed, data):
    """添加對沖解除建議欄位 (Hedge Unlocking)"""
    unlock = data.get("hedge_unlock")
    if not unlock:
        return

    symbol = data.get("symbol", "N/A")
    suggested_qty = unlock.get("reduce_spy_qty", 0)
    new_delta = unlock.get("new_delta", 0.0)
    reason = unlock.get("reason", "")
    risk_note = unlock.get("risk_note", "")

    # 依照使用者要求的文案格式
    unlock_text = (
        f"偵測到 **{symbol}** 強勢突破。目前您的 SPY 對沖正在產生 Hedge Drag。\n\n"
        f"✅ **建議動作：** 買回/平倉 `{suggested_qty}` 股 SPY。\n"
        f"🚀 **預計效應：** 釋放 Beta 動能，預計提升總組合 Delta 至 `{new_delta:+.1f}`。\n"
        f"🛡️ **防禦補償：** {risk_note}\n\u200b"
    )

    embed.add_field(name=f"🔓 對沖優化建議 ({reason})", value=unlock_text, inline=False)


def _add_ai_verification_fields(embed, data):
    """添加 AI 驗證決策欄位"""
    ai_decision = data.get("ai_decision")
    ai_reasoning = data.get("ai_reasoning")
    if ai_decision:
        if ai_decision == "APPROVE":
            ai_title = "🤖 Argo Cortex: ✅ 交易批准 (APPROVE)"
            ai_value = f"```\n{ai_reasoning}\n```"
        elif ai_decision == "VETO":
            ai_title = "🤖 Argo Cortex: ⛔ 否決交易 (VETO 黑天鵝警告)"
            ai_value = f"```diff\n- 警告: {ai_reasoning}\n```"
            embed.color = discord.Color.dark_red()
        elif ai_decision == "SKIP":
            ai_title = "🤖 Argo Cortex: ⚠️ 未啟用 (SKIP)"
            ai_value = f"```\n{ai_reasoning}\n```"
            embed.color = discord.Color.blue()

        embed.add_field(name=ai_title, value=ai_value, inline=False)


def get_ema_signal_ui(ema_signals: List[Dict[str, Any]]) -> str:
    """
    將 EMA 訊號清單轉化為 Discord 友善的文字流。
    """
    if not ema_signals:
        return "⚪ *暫無關鍵 EMA 觸碰訊號*"

    ui_lines = []
    for sig in ema_signals:
        window = sig["window"]
        sig_type = sig["type"]
        direction = sig["direction"]
        dist = sig["distance_pct"]

        # 1. 決定圖示與標題
        if sig_type == "CROSSOVER":
            icon = "🚀" if direction == "BULLISH" else "💀"
            action = "強勢突破" if direction == "BULLISH" else "失守跌破"
        else:  # TEST
            icon = "🛡️" if direction == "SUPPORT" else "🛑"
            action = "回測支撐" if direction == "SUPPORT" else "觸碰壓力"

        # 2. 格式化輸出
        line = f"{icon} **EMA {window} {action}** (偏離: `{dist}%`)"
        ui_lines.append(line)

    return "\n".join(ui_lines)


def _add_trend_and_support_fields(embed, data):
    """添加 EMA 狀態圖形化燈號欄位"""
    trend = data.get("trend", "UNKNOWN")
    ema21 = data.get("ema_21", 0.0)
    distance = data.get("distance_from_21", 0.0)

    if trend == "BULLISH_STRONG":
        trend_str = "📈 強勢多頭 (現價 > 8 > 21)"
    elif trend == "BULLISH_CORRECTION":
        trend_str = "📉 多頭回檔 (EMA 8 > 21 ≥ 現價)"
    elif trend == "BEARISH_STRONG":
        trend_str = "🐻 強勢空頭 (現價 < 8 < 21)"
    else:
        trend_str = "⚖️ 趨勢中性"

    # Risk 判定
    if distance > 10.0:
        risk_str = "⚠️ 過度擴張 (乖離率 > 10%)"
    elif distance < -10.0:
        risk_str = "⚠️ 超跌區間 (乖離率 < -10%)"
    else:
        risk_str = "✅ 穩定區間"

    support_str = f"EMA 21 位於 `${ema21:.2f}` (乖離: {distance:+.1f}%)"

    trend_info = f"**目前趨勢:** {trend_str}\n**支撐參考:** {support_str}\n**風險評估:** {risk_str}\n"
    trend_info += get_ema_signal_ui(data.get("ema_signals", []))

    embed.add_field(
        name="🧭 趨勢與支撐 (EMA 8/21)", value=trend_info + "\n\u200b", inline=False
    )


# ============================================================================
# Scan, intelligence, and market-report embeds
# ============================================================================


def create_sentiment_scan_embed(
    symbol: str,
    skew_data: dict,
    pcr_data: dict,
    uoa_data: list,
    max_pain_data: dict,
    iv_data: Optional[Any] = None,
) -> discord.Embed:
    """建立期權情緒掃描報告 Embed (繁體中文)"""
    title_suffix = ""
    is_premarket = False
    if iv_data:
        if hasattr(iv_data, "is_premarket"):
            is_premarket = iv_data.is_premarket
        elif isinstance(iv_data, dict):
            is_premarket = iv_data.get("is_premarket", False)

        current_iv_val = (
            iv_data.current_iv
            if hasattr(iv_data, "current_iv")
            else iv_data.get("current_iv", 0.0)
        )
        if is_premarket:
            if current_iv_val > 0.0:
                title_suffix = " [盤前/前日收盤]"
            else:
                title_suffix = " [盤前數據未更新]"

    embed = discord.Embed(
        title=f"📊 {symbol} 期權情緒掃描 (Sentiment Scan){title_suffix}",
        color=discord.Color.dark_magenta(),
        timestamp=datetime.now(timezone.utc),
    )

    if iv_data:
        if hasattr(iv_data, "current_iv"):
            current_iv = iv_data.current_iv
            iv_rank = iv_data.iv_rank
            iv_percentile = iv_data.iv_percentile
            expected_move_weekly = iv_data.expected_move_weekly
            iv_status = iv_data.iv_status
        else:
            current_iv = iv_data.get("current_iv", 0.0)
            iv_rank = iv_data.get("iv_rank", 0.0)
            iv_percentile = iv_data.get("iv_percentile", 0.0)
            expected_move_weekly = iv_data.get("expected_move_weekly", 0.0)
            iv_status = iv_data.get("iv_status", "Normal")

        iv_status_map = {
            "Low": "低 / 便宜",
            "Normal": "正常 / 公允",
            "High": "高 / 昂貴",
            "Extreme": "極高 / 泡沫",
        }
        status_tw = iv_status_map.get(iv_status, "正常 / 公允")

        if is_premarket and current_iv == 0.0:
            iv_lines = [
                "```ansi",
                f" 🌌 {symbol} 期權情緒掃描 (Sentiment Scan)",
                " ----------------------------------",
                " Implied Volatility (IV)",
                " └─ 值: \u001b[1;30m--%\u001b[0m (等待開盤 / 盤前未開市)",
                " IV Rank / IV Percentile",
                " └─ IV Rank: \u001b[1;30m--%\u001b[0m | IV Percentile: \u001b[1;30m--%\u001b[0m (狀態: 待開盤)",
                " Expected Move (預期震盪區間)",
                " └─ 本週預期: \u001b[1;30m--\u001b[0m (開盤後更新)",
                "```",
            ]
        elif is_premarket:
            iv_lines = [
                "```ansi",
                f" 🌌 {symbol} 期權情緒掃描 (Sentiment Scan)",
                " ----------------------------------",
                " Implied Volatility (IV)",
                f" └─ 值: {current_iv * 100:.1f}% (前日收盤 / 歷史波動率代理)",
                " IV Rank / IV Percentile",
                f" └─ IV Rank: {iv_rank:.1f}% | IV Percentile: {iv_percentile:.1f}% (狀態: {status_tw})",
                " Expected Move (預期震盪區間)",
                f" └─ 本週預期: ±${expected_move_weekly:.2f} (基於前收/HV計算)",
                "```",
            ]
        else:
            iv_lines = [
                "```ansi",
                f" 🌌 {symbol} 期權情緒掃描 (Sentiment Scan)",
                " ----------------------------------",
                " Implied Volatility (IV)",
                f" └─ 值: {current_iv * 100:.1f}% (當前 30 天平值期權隱含波動率)",
                " IV Rank / IV Percentile",
                f" └─ IV Rank: {iv_rank:.1f}% | IV Percentile: {iv_percentile:.1f}% (狀態: {status_tw})",
                " Expected Move (預期震盪區間)",
                f" └─ 本週預期: ±${expected_move_weekly:.2f} (基於當前 IV 計算)",
                "```",
            ]
        embed.add_field(
            name="📊 隱含波動率與預期區間", value="\n".join(iv_lines), inline=False
        )

    # Skew, PCR, Max Pain consolidated
    skew_val = skew_data.get("skew", 0)
    skew_state = skew_data.get("state", "N/A")
    pcr_val = pcr_data.get("pcr", 0)
    pcr_state = pcr_data.get("state", "N/A")
    mp_strike = max_pain_data.get("max_pain", "N/A")
    is_conv = "🎯 趨於收斂" if max_pain_data.get("is_converging") else "⏳ 尚有距離"

    metrics_lines = ["```ansi"]
    m_headers = ["指標項目", "數據值", "狀態 / 備註"]
    m_widths = [14, 10, 14]
    metrics_lines.append(
        " | ".join(
            _pad_string(h, w, "left" if i == 0 or i == 2 else "right")
            for i, (h, w) in enumerate(zip(m_headers, m_widths))
        )
    )
    metrics_lines.append("-" * (sum(m_widths) + 3 * (len(m_widths) - 1)))

    # Skew
    skew_val_str = f"{skew_val}%"
    metrics_lines.append(
        f"{_pad_string('Option Skew', m_widths[0])} | {_pad_string(skew_val_str, m_widths[1], 'right')} | {_pad_string(skew_state, m_widths[2])}"
    )
    # PCR
    pcr_val_str = f"{pcr_val}"
    metrics_lines.append(
        f"{_pad_string('Put/Call Ratio', m_widths[0])} | {_pad_string(pcr_val_str, m_widths[1], 'right')} | {_pad_string(pcr_state, m_widths[2])}"
    )
    # Max Pain
    mp_strike_str = f"${mp_strike}"
    metrics_lines.append(
        f"{_pad_string('Max Pain', m_widths[0])} | {_pad_string(mp_strike_str, m_widths[1], 'right')} | {_pad_string(is_conv, m_widths[2])}"
    )
    metrics_lines.append("```")
    embed.add_field(
        name="📐 期權情緒指標", value="\n".join(metrics_lines), inline=False
    )

    # UOA
    if uoa_data:
        uoa_lines = ["```ansi"]
        uoa_headers = ["到期日", "履約價", "類型", "機構/OI", "比例"]
        uoa_widths = [10, 7, 4, 15, 6]
        uoa_lines.append(
            " | ".join(
                _pad_string(h, w, "left" if i in (0, 2, 3) else "right")
                for i, (h, w) in enumerate(zip(uoa_headers, uoa_widths))
            )
        )
        uoa_lines.append("-" * (sum(uoa_widths) + 3 * (len(uoa_widths) - 1)))
        for item in uoa_data:
            expiry = str(item.get("expiry", "N/A"))
            strike = f"${item.get('strike', 'N/A')}"
            opt_type = str(item.get("type", "N/A")).upper()
            ratio = f"{item.get('ratio', 'N/A')}x"
            trade_type = str(item.get("trade_type", "SWEEP")).upper()
            oi_change = item.get("oi_change_net", 0)

            tag = "🔥 SWEEP" if trade_type == "SWEEP" else "📦 BLOCK"
            oi_change_str = f"{oi_change:+d}" if oi_change != 0 else "0"
            inst_str = f"{tag}({oi_change_str})"

            expiry = _visual_truncate(expiry, uoa_widths[0])
            strike = _visual_truncate(strike, uoa_widths[1])
            opt_type = _visual_truncate(opt_type, uoa_widths[2])
            inst_str = _visual_truncate(inst_str, uoa_widths[3])
            ratio = _visual_truncate(ratio, uoa_widths[4])

            row_str = " | ".join(
                [
                    _pad_string(expiry, uoa_widths[0], "left"),
                    _pad_string(strike, uoa_widths[1], "right"),
                    _pad_string(opt_type, uoa_widths[2], "left"),
                    _pad_string(inst_str, uoa_widths[3], "left"),
                    _pad_string(ratio, uoa_widths[4], "right"),
                ]
            )
            uoa_lines.append(row_str)
        uoa_lines.append("```")
        embed.add_field(
            name="🐋 異常活動 (UOA)", value="\n".join(uoa_lines), inline=False
        )
    else:
        embed.add_field(
            name="🐋 異常活動 (UOA)",
            value="```ansi\n目前無顯著異常活動\n```",
            inline=False,
        )

    embed.set_footer(text="Nexus Seeker | Volatility Strategist")
    return embed


def create_macro_scan_embed(
    macro_data: dict, alerts: Optional[List[Any]] = None
) -> discord.Embed:
    """建立巨觀環境與隔夜市場掃描 Embed (繁體中文)"""
    base_color = discord.Color.red() if alerts else discord.Color.blue()
    embed = discord.Embed(
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
    vix_color_start = " [0;31m" if vix > 25 else (" [0;33m" if vix > 20 else " [0;32m")
    vix_note = f"{vix_change:+.2f} ({vix_emoji})"
    vix_note_colored = f"{vix_change:+.2f} ({vix_color_start}{vix_emoji} [0m)"
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
        embed.add_field(
            name="✅ 巨觀狀態",
            value="殖利率曲線、匯率與波動率未見極端異常。維持標準市場部位。",
            inline=False,
        )

    embed.set_footer(text="Nexus Seeker | Global Macro Intelligence")
    return embed


def create_earnings_report_embed(
    report_type: str, report_content: str, raw_data: dict
) -> discord.Embed:
    """
    建立盤前財報與估值調整 Embed，沿用欄位化戰報風格。
    """
    upcoming = raw_data.get("upcoming_earnings", {})
    sentiment = raw_data.get("earnings_sentiment_scan", {})
    analyzed_symbols = int(raw_data.get("analyzed_symbols", 0) or 0)
    batch_label = _extract_report_batch(report_type)

    embed = discord.Embed(
        title="📊 Nexus Seeker 盤前財報與估值調整",
        description=(
            f"**更新批次：** {batch_label}\n"
            f"**掃描標的：** `{analyzed_symbols}` 檔 ｜ "
            f"**即將財報：** `{len(upcoming)}` 檔\n"
            "盤前聚焦財報日期、情緒與估值風險，維持與其他核心戰報一致的欄位式呈現。"
        ),
        color=_report_embed_color(report_content),
        timestamp=datetime.now(timezone.utc),
    )

    if upcoming:
        earnings_lines = ["```ansi"]
        headers = ["標的", "財報日", "情緒覆蓋"]
        widths = [8, 14, 12]
        earnings_lines.append(
            " | ".join(_pad_string(h, w) for h, w in zip(headers, widths))
        )
        earnings_lines.append("-" * (sum(widths) + 3 * (len(widths) - 1)))
        for sym, events in upcoming.items():
            for event in events:
                date = event.get("date", "未知日期")
                sentiment_status = "新聞+社群" if sym in sentiment else "日曆"
                earnings_lines.append(
                    " | ".join(
                        [
                            _pad_string(_visual_truncate(sym, widths[0]), widths[0]),
                            _pad_string(_visual_truncate(date, widths[1]), widths[1]),
                            _pad_string(
                                _visual_truncate(sentiment_status, widths[2]), widths[2]
                            ),
                        ]
                    )
                )
        earnings_lines.append("```")
        embed.add_field(
            name="📅 即將發布財報標的",
            value=_safe_embed_field_value("\n".join(earnings_lines), "近期無財報事件"),
            inline=False,
        )
    else:
        embed.add_field(
            name="📅 即將發布財報標的",
            value=_safe_embed_field_value(
                "近期無需調整倉位的財報事件。", "近期無財報事件"
            ),
            inline=False,
        )

    sentiment_lines = []
    for sym, payload in list(sentiment.items())[:3]:
        news = str(payload.get("news", "無相關資訊"))
        reddit = str(payload.get("reddit_sentiment", "無相關資訊"))
        sentiment_lines.append(f"**{sym}**")
        sentiment_lines.append(f"📰 新聞：{_truncate_with_boundary(news, 140)}")
        sentiment_lines.append(f"💬 社群：{_truncate_with_boundary(reddit, 140)}")
        sentiment_lines.append("")
    if sentiment_lines:
        sentiment_lines.pop()
    else:
        sentiment_lines = ["目前無額外新聞 / Reddit 情緒補充。"]
    embed.add_field(
        name="🧠 情緒 / 估值快照",
        value=_safe_embed_field_value("\n".join(sentiment_lines), "目前無額外情緒資訊"),
        inline=False,
    )

    note = raw_data.get("note", "")
    if note:
        embed.add_field(
            name="🧾 掃描備註",
            value=_safe_embed_field_value(str(note), "無補充備註"),
            inline=False,
        )

    _append_ai_report_fields(embed, report_content)
    embed.set_footer(text="Nexus Seeker AI Analyst | 盤前財報與估值調整")
    return embed


def create_sector_flow_report_embed(
    report_type: str, report_content: str, raw_data: dict
) -> discord.Embed:
    """建立收盤資金流向與板塊輪動報告 Embed。"""
    batch_label = _extract_report_batch(report_type)
    vix = _safe_float(raw_data.get("vix"))
    spy_price = _safe_float(raw_data.get("spy_price"))
    vix_tier_name = str(raw_data.get("vix_tier_name", "Unknown"))
    sectors = raw_data.get("sectors", []) or []
    poly_events = raw_data.get("poly_events", []) or []
    spy_max_pain = raw_data.get("spy_max_pain", {}) or {}

    embed = discord.Embed(
        title="📊 Nexus Seeker 收盤資金流向與板塊輪動報告",
        description=(
            f"**更新批次：** {batch_label}\n"
            f"**SPY 現價：** `${spy_price:.2f}` ｜ "
            f"**VIX：** `{vix:.2f}` (`{vix_tier_name}`)\n"
            "沿用欄位式收盤戰報版型，彙整板塊輪動、事件定價與 AI 收斂結論。"
        ),
        color=_report_embed_color(report_content),
        timestamp=datetime.now(timezone.utc),
    )

    market_panel_body = "\n".join(
        [
            f"SPY 現價: ${spy_price:.2f}",
            f"VIX: {vix:.2f} ({vix_tier_name})",
            f"掃描板塊數: {len(sectors)}",
            f"Polymarket 訊號: {len(poly_events)}",
        ]
    )
    market_panel = _build_watchlist_style_panel(
        "🌐 收盤市場快照 (Close Snapshot)",
        market_panel_body,
        width=45,
        empty_msg="暫無市場快照",
    )
    embed.add_field(
        name="🌐 收盤市場快照",
        value=_safe_embed_codeblock_value(market_panel, "暫無市場快照", lang="ansi"),
        inline=False,
    )

    if sectors:
        sector_lines = ["```ansi"]
        sector_lines.append(" 🔄 板塊輪動快照 (Sector Rotation)")
        sector_lines.append(" ----------------------------------")
        headers = ["ETF", "板塊", "日變動", "量比", "Skew", "UOA"]
        widths = [5, 18, 8, 6, 8, 4]
        sector_lines.append(
            " ".join(
                [
                    " | ".join(_pad_string(h, w) for h, w in zip(headers, widths)),
                ]
            )
        )
        sector_lines.append("-" * (sum(widths) + 3 * (len(widths) - 1)))
        sorted_sectors = sorted(
            sectors,
            key=lambda item: abs(_safe_float(item.get("pct_change"))),
            reverse=True,
        )
        for item in sorted_sectors:
            sector_lines.append(
                " | ".join(
                    [
                        _pad_string(
                            _visual_truncate(str(item.get("symbol", "N/A")), widths[0]),
                            widths[0],
                        ),
                        _pad_string(
                            _visual_truncate(str(item.get("name", "N/A")), widths[1]),
                            widths[1],
                        ),
                        _pad_string(
                            f"{_safe_float(item.get('pct_change')):+.2f}%",
                            widths[2],
                            "right",
                        ),
                        _pad_string(
                            f"{_safe_float(item.get('rel_vol')):.2f}x",
                            widths[3],
                            "right",
                        ),
                        _pad_string(
                            f"{_safe_float(item.get('skew')):+.1f}",
                            widths[4],
                            "right",
                        ),
                        _pad_string(
                            str(int(item.get("uoa_count", 0))), widths[5], "right"
                        ),
                    ]
                )
            )
        sector_lines.append("```")
        sector_value = "\n".join(sector_lines)
    else:
        sector_panel = _build_watchlist_style_panel(
            "🔄 板塊輪動快照 (Sector Rotation)",
            "",
            width=45,
            empty_msg="目前無板塊輪動資料。",
        )
        sector_value = f"```ansi\n{sector_panel}\n```"
    embed.add_field(
        name="🔄 板塊輪動快照",
        value=_safe_embed_field_value(sector_value, "目前無板塊輪動資料。"),
        inline=False,
    )

    event_bullets: list[str] = []
    max_pain_value = spy_max_pain.get("max_pain")
    if max_pain_value is not None:
        event_bullets.append(f"SPY Max Pain: ${_safe_float(max_pain_value):.2f}")

    for event in poly_events[:3]:
        question = _truncate_with_boundary(str(event.get("question", "N/A")), 140)
        event_bullets.append(f"Polymarket: {question}")

    event_panel = _build_watchlist_style_panel(
        "🐋 事件定價與關鍵參考 (Event Pricing)",
        "\n".join(event_bullets),
        width=45,
        empty_msg="目前無顯著 Polymarket / Max Pain 補充訊號。",
    )
    embed.add_field(
        name="🐋 事件定價與關鍵參考",
        value=_safe_embed_codeblock_value(
            event_panel, "目前無事件定價資料", lang="ansi"
        ),
        inline=False,
    )

    sections = _parse_ai_report_sections(report_content)
    if sections:
        for header, content in sections:
            panel = _build_watchlist_style_panel(
                header,
                content,
                width=45,
                empty_msg="無詳細資訊",
            )
            embed.add_field(
                name=header,
                value=_safe_embed_codeblock_value(panel, "無詳細資訊", lang="ansi"),
                inline=False,
            )
    else:
        panel = _build_watchlist_style_panel(
            "🤖 AI 分析摘要 (AI Summary)",
            report_content,
            width=45,
            empty_msg="無詳細資訊",
        )
        embed.add_field(
            name="🤖 AI 分析摘要",
            value=_safe_embed_codeblock_value(panel, "無詳細資訊", lang="ansi"),
            inline=False,
        )
    embed.set_footer(text="Nexus Seeker AI Analyst | 收盤資金流向與板塊輪動")
    return embed


def _add_sentiment_fields(embed, data):
    """添加期權情緒指標到掃描報告"""
    skew_val = data.get("skew")
    pcr_val = data.get("pcr")
    uoa_list = data.get("uoa_list", [])

    if skew_val is not None or pcr_val is not None or uoa_list:
        content = ""
        if skew_val is not None:
            content += f"📐 **Skew:** `{skew_val}%` | "
        if pcr_val is not None:
            content += f"⚖️ **PCR:** `{pcr_val}`"

        if uoa_list:
            content += "\n🐋 **UOA 偵測:** `FOUND` | 信心: `HIGH`"
            if data.get("high_conviction"):
                content += " | 🔥 **高信心訊號**"

        if content:
            embed.add_field(
                name="🎭 期權市場情緒 (Sentiment)",
                value=content + "\n\u200b",
                inline=False,
            )


def create_scan_embed(data, user_capital=100000.0):
    strategy = data.get("strategy", "UNKNOWN")
    stock_cost = data.get("stock_cost", 0.0)

    embed, is_covered = _build_embed_base(data, strategy, stock_cost)

    # VIX tier color override
    vix_color = data.get("vix_tier_color") or (
        data.get("vix_battle_status", {}).get("color_hex")
    )
    if vix_color:
        embed.color = discord.Color(vix_color)

    # Render UI fields (VIX Battle Status first)
    _add_vix_battle_status_field(embed, data)

    # 🚀 整合 Gap & Fill 狀態 (New Engine)
    gap = data.get("gap_status")
    if gap:
        from market_analysis.gap_analysis import GapStatus

        status_emoji = {
            GapStatus.GAP_HOLDING: "🟢 持續跳空 (Holding)",
            GapStatus.PARTIAL_FILL: "🟡 部分回補 (Filling)",
            GapStatus.FULL_FILL: "🔴 完全回補 (Filled)",
            GapStatus.NO_GAP: "⚪ 無跳空",
        }.get(gap.current_fill_status, "⚪ N/A")

        support_tag = " | 🛡️ 支撐已確認" if gap.is_support_confirmed else ""
        gap_color = "🟢" if gap.gap_size > 0 else "🔴"

        gap_info = (
            f"{gap_color} **{'向上跳空 (UP-GAP)' if gap.gap_size > 0 else '向下跳空 (DOWN-GAP)'}**: `{gap.gap_pct:+.2f}%` (${gap.gap_size:+.2f})\n"
            f"**狀態:** {status_emoji}{support_tag}\n"
            f"**區間:** `${gap.gap_zone[0]:.2f}` - `${gap.gap_zone[1]:.2f}`\n\u200b"
        )
        embed.add_field(name="📈 Gap & Fill 跳空監控", value=gap_info, inline=False)

    _add_market_overview_fields(embed, data)
    _add_volatility_fields(embed, data, strategy)
    _add_sentiment_fields(embed, data)
    _add_trend_and_support_fields(embed, data)
    _add_performance_and_kelly_fields(embed, data, user_capital)
    _add_earnings_fields(embed, data, strategy)

    if is_covered:
        _add_covered_call_fields(embed, data, stock_cost)

    _add_expected_move_fields(embed, data, strategy, is_covered)
    _add_liquidity_fields(embed, data)
    _add_strategy_upgrade_fields(embed, data, strategy)

    # 🚀 執行優化回饋顯示
    _add_risk_optimization_fields(embed, data, user_capital)
    _add_hedge_unlock_fields(embed, data)

    add_news_field(embed, data.get("news_text"))
    add_reddit_field(embed, data.get("reddit_text"))
    _add_ai_verification_fields(embed, data)

    # 🚀 AlertFilter 推播理由 (僅在條件式過濾觸發時顯示)
    alert_reason = data.get("alert_reason")
    if alert_reason:
        embed.add_field(
            name="📢 推播觸發條件",
            value=f"```\n{alert_reason}\n```",
            inline=False,
        )

    vix_spot = data.get("vix_spot") or (
        data.get("vix_battle_status", {}).get("vix_spot")
    )
    vix_emoji = data.get("vix_tier_emoji") or (
        data.get("vix_battle_status", {}).get("emoji", "")
    )
    vix_name = data.get("vix_tier_name") or (
        data.get("vix_battle_status", {}).get("name", "")
    )
    vix_footer = f" | VIX: {vix_spot:.1f} {vix_emoji} {vix_name}" if vix_spot else ""
    embed.set_footer(
        text=f"Nexus Seeker 風控引擎 • 基準 SPY: ${data.get('spy_price', 500):.1f}{vix_footer}"
    )
    return embed


def create_psq_embed(data: dict) -> discord.Embed:
    """建構獨立的 PowerSqueeze (PSQ) 戰情報告 Embed"""
    sym = data.get("symbol", "UNKNOWN")
    psq = data.get("psq_result")

    if not psq:  # fallback
        return discord.Embed(
            title=f"⚡ PowerSqueeze 戰情報告 | {sym}",
            description="無可用數據",
            color=discord.Color.dark_grey(),
        )

    color = discord.Color.purple() if psq.is_squeezing else discord.Color.dark_teal()
    if psq.is_breakout_long:
        color = discord.Color.green()
    elif psq.is_breakout_short:
        color = discord.Color.red()

    embed = discord.Embed(
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
    vix_tf_note = psq.vix_timeframe_note if hasattr(psq, "vix_timeframe_note") else ""

    if vix_label != "NORMAL":
        label_map = {
            "OVEREXTENDED_RISK": "⚠️ **過度延伸風險** | 低 VIX 環境多頭訊號可能是牛陷阱",
            "HIGH_CONVICTION_RECOVERY": "🚀 **高確信反彈** | 高 VIX + 空頭減速 = 反轉機會",
        }
        label_text = label_map.get(vix_label, vix_label)
        embed.add_field(name="🏅 VIX 動能判定", value=label_text, inline=False)

    if vix_tf_note:
        embed.add_field(name="⏱️ 時間框架建議", value=f"`{vix_tf_note}`", inline=False)

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


def create_news_scan_embed(symbol, news_text):
    """建構新聞掃描結果的 Embed"""
    embed = discord.Embed(title=f"📰 {symbol} 官方新聞掃描", color=discord.Color.blue())
    add_news_field(embed, news_text)
    embed.set_footer(text="Nexus Seeker 研報系統 • 資料來源: Yahoo Finance")
    return embed


def create_reddit_scan_embed(symbol, reddit_text):
    """建構 Reddit 情緒掃描結果的 Embed"""
    embed = discord.Embed(
        title=f"🔥 {symbol} 散戶情緒優勢 (Reddit 同步)", color=discord.Color.orange()
    )
    add_reddit_text = reddit_text
    add_reddit_field(embed, add_reddit_text)
    embed.set_footer(
        text="Nexus Seeker 研報系統 • 資料來源: Reddit (WSB/Stocks/Options)"
    )
    return embed


def create_media_sentiment_embed(symbol, news_text, reddit_text):
    """建構輿情與社群 (Media & Social) 掃描結果的統一 Embed"""
    embed = discord.Embed(
        title=f"🎭 {symbol} 輿情與社群大盤掃描 (Media & Social)",
        color=discord.Color.blue(),
    )
    add_news_field(embed, news_text)
    add_reddit_field(embed, reddit_text)
    embed.set_footer(
        text="Nexus Seeker 輿情中心 • 資料來源: Yahoo Finance & Reddit (WSB/Stocks/Options)"
    )
    return embed


def create_polymarket_list_embed(markets: List[Dict[str, Any]]):
    """建構 Polymarket 監控中的熱門市場 Embed"""
    embed = discord.Embed(
        title="🐋 Polymarket 巨鯨意圖圖譜",
        color=discord.Color.blue(),
        timestamp=datetime.now(timezone.utc),
    )

    if not markets:
        embed.description = "目前沒有監控中的市場。"
        return embed

    description_lines = ["```ansi"]
    for i, m in enumerate(markets, 1):
        question = m.get("question", "未知市場")
        # 截斷過長的標題
        if len(question) > 55:
            question = question[:52] + "..."

        # 取得 token 價格資訊 (如果有的話)
        tokens = m.get("tokens", [])
        price_info = ""
        if tokens:
            # 顯示前兩個 outcome 的價格
            p_list = []
            for t in tokens[:2]:
                outcome = str(t.get("outcome", "")).strip()
                price = t.get("price", 0)

                # 排除單字元的雜訊 (例如 [ or " )
                if len(outcome) <= 1 and outcome not in ["?", "是", "否"]:
                    continue

                # 簡單格式化價格 (0-1)
                try:
                    price_val = float(price)
                    price_str = f"{price_val:.2f}"
                except Exception:
                    price_str = str(price)

                if outcome:
                    p_list.append(f"{outcome}: {price_str}")

            if p_list:
                price_info = " | ".join(p_list)

        description_lines.append(f"{i:2d}. {question}")
        if price_info:
            description_lines.append(f"    └─ {price_info}")
        else:
            description_lines.append("")

    if len(description_lines) > 1 and description_lines[-1] == "":
        description_lines.pop()
    description_lines.append("```")

    full_desc = "\n".join(description_lines)
    # 檢查總長度，避免超過 Discord 限制
    if len(full_desc) > 3900:
        full_desc = full_desc[:3890] + "\n...\n```"

    embed.description = full_desc
    embed.set_footer(text="Nexus Seeker | Polymarket Monitor (Top 20 Active Markets)")
    return embed


def create_polymarket_status_embed(status: Dict[str, Any]) -> discord.Embed:
    """建構 Polymarket 服務狀態 Embed。"""
    embed = discord.Embed(
        title="【 🐋 Polymarket 服務狀態 】",
        color=discord.Color.green() if status["connected"] else discord.Color.red(),
        timestamp=datetime.now(timezone.utc),
    )

    status_emoji = "🟢 已連線" if status["connected"] else "🔴 斷線中"
    running_emoji = "✅ 運行中" if status["running"] else "🛑 已停止"
    content = [
        "## 🖥️ 監控系統運行資訊",
        "---",
        f"**服務狀態：** {running_emoji}",
        f"**連線狀態：** {status_emoji}",
        f"**訂閱資產：** `{status['asset_count']}` 個標的",
        f"**最後訊息：** {status['last_message']}",
        f"**異常計數：** `{status['errors']}` 次",
        "---",
    ]
    embed.description = "\n".join(content)
    embed.set_footer(text="Nexus Seeker | Polymarket Monitor")
    return embed


def create_quote_embed(symbol: str, data: Dict[str, Any]) -> discord.Embed:
    """建構即時報價 Embed。"""
    embed = discord.Embed(
        title=f"💹 {symbol} 即時報價 (Real-time Quote)",
        color=discord.Color.blue() if data["dp"] >= 0 else discord.Color.red(),
        timestamp=datetime.now(timezone.utc),
    )
    embed.add_field(name="現價 (Current)", value=f"**${data['c']}**", inline=True)
    embed.add_field(name="漲跌幅 (%)", value=f"`{data['dp']}%`", inline=True)
    embed.add_field(
        name="今日高/低", value=f"H: `${data['h']}` / L: `${data['l']}`", inline=False
    )
    embed.add_field(name="前收盤 (PC)", value=f"`${data['pc']}`", inline=True)
    embed.set_footer(text="Nexus Seeker | Market Intelligence Feed")
    return embed


# ============================================================================
# Trading, risk, and alert embeds
# ============================================================================


def create_profit_lock_alert_embed(event: Dict[str, Any]) -> discord.Embed:
    """建立 DITM 獲利鎖定警報 Embed。"""
    embed = discord.Embed(
        title="🚨 DITM 凸性防護：獲利鎖定已觸發",
        description=(
            f"偵測到標的 **{event['symbol']}** 已進入深價內 (DITM)，"
            "凸性消失且風險報酬比惡化。"
        ),
        color=discord.Color.gold(),
        timestamp=datetime.now(timezone.utc),
    )
    embed.add_field(
        name="觸發指標",
        value=f"```\n未實現損益: {event['pnl_pct']}% | DTE: {event['dte']}\n```",
        inline=False,
    )
    embed.add_field(name="執行指令", value="✅ **獲利鎖定 (Profit Lock)**", inline=True)
    embed.add_field(name="核心邏輯", value=event["reason"], inline=False)
    embed.set_footer(text="Mission-Critical Risk Environment | Nexus Seeker")
    return embed


def create_gamma_fragility_embed(event: Dict[str, Any]) -> discord.Embed:
    """建立 Gamma 脆弱性警告 Embed。"""
    embed = discord.Embed(
        title="🆘 Gamma 脆弱性警告 (Net Gamma < -20)",
        description="偵測到投資組合淨 Gamma 已跌破臨界點，曝險加速度呈非線性擴張。",
        color=discord.Color.dark_red(),
        timestamp=datetime.now(timezone.utc),
    )
    embed.add_field(name="目前淨 Gamma", value=f"`{event['net_gamma']}`", inline=True)
    embed.add_field(name="安全臨界點", value=f"`{event['threshold']}`", inline=True)
    embed.add_field(
        name="優先指令",
        value="🛡️ **注入正 Gamma 緩衝 (買入近月 ATM 期權) 或 立即減倉**",
        inline=False,
    )
    embed.set_footer(text="Fragility Guard Engine v2.0 | Nexus Seeker")
    return embed


def create_pre_market_earnings_embed(
    alerts: List[Dict[str, Any]], scanned_symbols: List[str], warning_days: int
) -> discord.Embed:
    """建立盤前財報雷達通知 Embed。"""
    if alerts:
        description = "\n\n".join(
            (
                f"**{item['symbol']}** "
                f"({'⚠️ **持倉高風險**' if item['is_portfolio'] else '👀 觀察清單'})\n"
                f"└ 📅 財報日: `{item['earnings_date']}` (倒數 **{item['days_left']}** 天)"
            )
            for item in alerts
        )
        return discord.Embed(
            title="🚨 【盤前財報季雷達預警】",
            description=description,
            color=discord.Color.red(),
            timestamp=datetime.now(timezone.utc),
        )

    scanned_list = "、".join(f"`{symbol}`" for symbol in scanned_symbols)
    return discord.Embed(
        title="✅ 【盤前財報季雷達掃描完畢】",
        description=f"已掃描：{scanned_list}\n\n近 {warning_days} 日內無財報風險，安全過關！",
        color=discord.Color.green(),
        timestamp=datetime.now(timezone.utc),
    )


def create_ditm_transition_alert_embed(
    *,
    symbol: str,
    exit_reason: str,
    action_taken: str,
    pnl: float,
    exposure_pct: float,
    hedge: Optional[Dict[str, Any]] = None,
) -> discord.Embed:
    """建立 VTR DITM 防禦通知 Embed。"""
    embed = discord.Embed(
        title="🚨 NRO 優先指令：Profit Lock (DITM 凸性防禦)",
        description=f"偵測到標的 **{symbol}** 已進入深價內 (DITM)，凸性消失且風險報酬比惡化。",
        color=discord.Color.gold(),
        timestamp=datetime.now(timezone.utc),
    )
    embed.add_field(name="觸發指標", value=f"```\n{exit_reason}\n```", inline=False)
    embed.add_field(name="執行動作", value=f"✅ **{action_taken}**", inline=True)
    embed.add_field(name="鎖定利潤", value=f"💰 `${pnl:.2f}`", inline=True)
    embed.add_field(
        name="帳戶目前總曝險",
        value=f"`{exposure_pct:.2f}%` (Beta-Weighted Delta)",
        inline=False,
    )
    if hedge:
        embed.add_field(
            name="🛡️ NRO 對沖建議",
            value=f"{hedge['action']} (缺口: `{hedge['gap']}`)",
            inline=False,
        )
    embed.set_footer(text="Quantitative Defense Pipeline | Nexus Risk Optimizer")
    return embed


def create_intraday_execution_guide_embed(
    *,
    phase_name: str,
    vix: float,
    memory_percent: float,
    is_memory_gated: bool,
    vix_level_name: str = "",
    greeks_status: str = "",
    runway_days: float = 0.0,
    theta_cov: float = 0.0,
    active_signal_content: str = "",
    sma_cache_size: int = 0,
    ema_cache_size: int = 0,
) -> discord.Embed:
    """建立盤中量化執行指引 Embed。"""
    embed = discord.Embed(
        title=f"🛡️ 盤中量化執行指引 - {phase_name}",
        color=discord.Color.red() if vix > 25 else discord.Color.blue(),
        timestamp=datetime.now(timezone.utc),
    )

    if is_memory_gated:
        embed.description = (
            "⚠️ **Memory Safety Gate Active**: VPS RAM > 85%。"
            "已暫停部分耗能分析以保證風控引擎穩定。"
        )
        embed.add_field(
            name="系統狀態", value=f"RAM: `{memory_percent}%`", inline=False
        )
        embed.set_footer(text="Nexus Seeker | NRO Vanna-Aware Intelligence")
        return embed

    embed.add_field(
        name="1️⃣ 風險狀態 (Risk Status)",
        value=f"**VIX 階級:** {vix_level_name} ({vix:.1f})\n**Greeks 完整性:** {greeks_status}",
        inline=False,
    )
    embed.add_field(
        name="2️⃣ 財務健康 (Financial Health)",
        value=f"**剩餘跑道:** `{runway_days:.1f}` 天\n**Theta 覆蓋率:** `{theta_cov:.1f}%`",
        inline=False,
    )
    embed.add_field(
        name="3️⃣ 活躍信號 (Active Signal)",
        value=active_signal_content,
        inline=False,
    )
    embed.add_field(
        name="4️⃣ 系統狀態 (System Health)",
        value=f"RAM: `{memory_percent}%` | BoundedCache (SMA/EMA): `{sma_cache_size}/{ema_cache_size}`",
        inline=False,
    )
    embed.set_footer(text="Nexus Seeker | NRO Vanna-Aware Intelligence")
    return embed


def create_vtr_settlement_notice_embed(
    *,
    status_icon: str,
    symbol: str,
    pnl: float,
    exposure_pct: float,
    regime: Optional[str] = None,
    target_delta: Optional[float] = None,
    hedge: Optional[Dict[str, Any]] = None,
) -> discord.Embed:
    """建立 VTR 轉倉/平倉結算通知 Embed。"""
    embed = discord.Embed(
        title=f"{status_icon} {symbol} 結算通知",
        color=discord.Color.blue() if "轉倉" in status_icon else discord.Color.red(),
        timestamp=datetime.now(timezone.utc),
    )
    embed.add_field(name="損益", value=f"`${pnl:.2f}`", inline=True)
    embed.add_field(
        name="目前總曝險",
        value=f"`{exposure_pct:.2f}%` (Beta-Weighted Delta)",
        inline=True,
    )

    if regime is not None and target_delta is not None:
        embed.add_field(name="🧠 系統自主位階判定", value=f"`{regime}`", inline=False)
        embed.add_field(
            name="理想總曝險目標", value=f"`{target_delta:.1f} Delta`", inline=True
        )
    if hedge:
        embed.add_field(
            name="🛡️ 自動對沖決策",
            value=f"{hedge['action']} (缺口: `{hedge['gap']}`)",
            inline=False,
        )
    embed.set_footer(text="GhostTrader | Settlement Notice")
    return embed


# ============================================================================
# Portfolio, watchlist, and terminal embeds
# ============================================================================


def create_holdings_embed(
    holdings_data: List[Dict[str, Any]], total_capital: float
) -> discord.Embed:
    """建構現貨持倉 (Holdings) 狀態報告 Embed"""
    embed = discord.Embed(
        title="📊 Nexus Seeker | 現貨持倉清單",
        description="追蹤您的長期股權資產與成本分佈。\n\u200b",
        color=discord.Color.blue(),
        timestamp=datetime.now(timezone.utc),
    )

    if not holdings_data:
        embed.description = "📭 目前無現貨持倉紀錄。請使用 `/add_holding` 進行登錄。"
        return embed

    # A-Z sort by symbol
    sorted_holdings = sorted(holdings_data, key=lambda x: x.get("symbol", "").upper())

    total_value = 0.0
    total_pnl = 0.0

    data_lines = []
    header = f"{_pad_string('標的', 8)} | {_pad_string('數量', 8, 'right')} | {_pad_string('平均成本', 10, 'right')} | {_pad_string('現價', 10, 'right')} | {_pad_string('當前損益', 10, 'right')}"
    divider = "-" * 58

    for h in sorted_holdings:
        curr_p = h.get("current_price", 0.0)
        pnl = (curr_p - h["avg_cost"]) * h["quantity"] if curr_p > 0 else 0.0
        pnl_pct = (
            (curr_p / h["avg_cost"] - 1) if h["avg_cost"] > 0 and curr_p > 0 else 0.0
        )

        total_pnl += pnl
        total_value += curr_p * h["quantity"]

        sym = _pad_string(h["symbol"], 8)
        qty = _pad_string(f"{h['quantity']:,.0f}", 8, "right")
        cost = _pad_string(f"${h['avg_cost']:,.2f}", 10, "right")
        curr_price_str = f"${curr_p:,.2f}"
        curr_p_fmt = _pad_string(curr_price_str, 10, "right")

        # 使用 ANSI 顏色：綠色表示正損益，紅色表示負損益
        color_start = "\u001b[0;32m" if pnl >= 0 else "\u001b[0;31m"
        pnl_pct_str = f"{pnl_pct:+.1%}"
        pnl_fmt = _pad_string(pnl_pct_str, 10, "right").replace(
            pnl_pct_str, f"{color_start}{pnl_pct_str}\u001b[0m"
        )

        data_lines.append(f"{sym} | {qty} | {cost} | {curr_p_fmt} | {pnl_fmt}")

    chunks = _chunk_ansi_table(header, divider, data_lines)
    for i, chunk in enumerate(chunks):
        name = (
            f"📦 持倉明細 ({i+1}/{len(chunks)})" if len(chunks) > 1 else "📦 持倉明細"
        )
        embed.add_field(name=name, value=chunk, inline=False)

    summary = (
        f"💰 **持倉總市值**: `${total_value:,.2f}`\n"
        f"📈 **累計未實現損益**: `${total_pnl:,.2f}`\n"
        f"⚖️ **佔總預算比例**: `{ (total_value / total_capital * 100) if total_capital > 0 else 0:.1f}%`"
    )
    embed.add_field(name="🏁 財務摘要", value=summary, inline=False)

    embed.set_footer(text="Nexus Accounting Engine | 專業股權追蹤")
    return embed


def create_trades_embed(
    pnl_data: Dict[str, Any],
    total_capital: float = 0.0,
) -> discord.Embed:
    """建構實單持倉 (Portfolio) 狀態與未實現損益報告 Embed"""
    embed = discord.Embed(
        title="📊 Nexus Seeker | 實單持倉清單 (包含帳面損益)",
        description="追蹤您的期權實單持倉與即時未實現損益 (Unrealized PnL)。\n\u200b",
        color=discord.Color.green(),
        timestamp=datetime.now(timezone.utc),
    )

    trades = pnl_data.get("trades", [])
    if not trades:
        embed.description = "📭 目前無持倉紀錄。"
        return embed

    data_lines = []
    # 標頭 (調整 Python len 以匹配可見寬度)
    header = f"{'ID'.ljust(2)} | {'標的'.ljust(4)} | {'到期日'.ljust(7)} | {'履約'.ljust(5)} | {'數'.rjust(1)} | {'成本'.rjust(4)} | {'現價'.rjust(4)} | {'帳面損益'.rjust(10)}"
    divider = "-" * 75

    total_cost = 0.0
    for t in trades:
        trade_id = t["id"]
        sym = t["symbol"]
        o_type = t["opt_type"]
        strike = t["strike"]
        exp = t["expiry"]
        qty = t["quantity"]
        entry_p = t["entry_price"]
        curr_p = t.get("current_price", 0.0)
        unrealized_pnl = t["unrealized_pnl"]
        pnl_pct = t["pnl_pct"]

        total_cost += abs(entry_p * qty * 100)

        id_fmt = f"{trade_id:02d}".ljust(2)
        sym_fmt = sym.ljust(4)
        exp_fmt = exp.ljust(10)
        st_type_fmt = f"{strike}{o_type[0].upper()}".ljust(7)

        color_code = "\x1b[0;32m" if qty > 0 else "\x1b[0;31m"
        qty_val = f"{abs(qty):>2}"
        qty_fmt = f"{color_code}{qty_val}\x1b[0m"

        cost_fmt = f"{entry_p:6.2f}"
        curr_fmt = f"{curr_p:6.2f}"

        pnl_color = "\x1b[0;32m" if unrealized_pnl >= 0 else "\x1b[0;31m"
        pnl_val = f"${unrealized_pnl:+.0f} ({pnl_pct:+.1%})"
        pnl_fmt = f"{pnl_color}{pnl_val:>14}\x1b[0m"

        data_lines.append(
            f"{id_fmt} | {sym_fmt} | {exp_fmt} | {st_type_fmt} | {qty_fmt} | {cost_fmt} | {curr_fmt} | {pnl_fmt}"
        )

    chunks = _chunk_ansi_table(header, divider, data_lines)
    for i, chunk in enumerate(chunks):
        name = (
            f"📦 持倉明細 ({i+1}/{len(chunks)})" if len(chunks) > 1 else "📦 持倉明細"
        )
        embed.add_field(name=name, value=chunk, inline=False)

    total_unrealized_pnl = pnl_data.get("total_unrealized_pnl", 0.0)

    summary = (
        f"💰 **持倉總權利金成本 (概算)**: `${total_cost:,.2f}`\n"
        f"⚖️ **佔總預算比例**: `{ (total_cost / total_capital * 100) if total_capital > 0 else 0:.1f}%`\n"
        f"📈 **總未實現損益 (Unrealized PnL)**: `${total_unrealized_pnl:,.2f}`"
    )
    embed.add_field(name="🏁 財務摘要 (Financial Summary)", value=summary, inline=False)

    embed.set_footer(text="Nexus Portfolio Engine | 專業實單與損益監控")
    return embed


def create_strategic_dash_embed(
    user_ctx: Any, pnl_data: Dict[str, Any], vix_spot: float = 18.0
) -> discord.Embed:
    """
    建構戰略看板 (Strategic Dashboard) Embed.
    遵循 Task 1 的 Traditional Chinese 模板。
    """
    embed = discord.Embed(
        title="📊 Nexus 交易員戰略看板",
        color=discord.Color.dark_blue(),
        timestamp=datetime.now(timezone.utc),
    )

    # 1. 財務生存狀態 (Financial Runway)
    daily_theta = _safe_float(user_ctx.total_theta)
    monthly_expense = _safe_float(user_ctx.monthly_expense)
    daily_expense = monthly_expense / 30.0 if monthly_expense > 0 else 0.0
    coverage_pct = (daily_theta / daily_expense * 100) if daily_expense > 0 else 100.0

    # 計算跑道天數
    gap = daily_expense - daily_theta
    cash_reserve = _safe_float(user_ctx.cash_reserve)
    if gap <= 0:
        runway_days = "∞ (收益已覆蓋支出)"
    else:
        runway_days = f"{cash_reserve / gap:,.0f}" if gap > 0 else "∞"

    status_mode = "觀戰模式" if not user_ctx.is_professional_mode else "實戰模式"
    nav = _safe_float(user_ctx.capital) + pnl_data.get("total_unrealized_pnl", 0.0)

    runway_info = (
        f"* **總資產 (NAV):** `${nav:,.0f}` ({status_mode})\n"
        f"* **目前跑道:** {runway_days} 天 (由現有現金與 Theta 收益推算)\n"
        f"* **收租效率:** 每日 Theta `${daily_theta:,.2f}` (覆蓋率 {coverage_pct:.1f}%)\n"
    )
    if coverage_pct < 100 and daily_expense > 0:
        runway_info += f"> 💡 警訊：現金流覆蓋不足，每日收租缺口為 `${gap:,.2f}`。"

    embed.add_field(
        name="🏁 財務生存狀態 (Financial Runway)", value=runway_info, inline=False
    )

    # 2. 組合風險精算 (NRO Integrity)
    # Vanna Sensitivity Stress Test: 若 VIX 上升 10%，隱含 Delta 將變動至 [New_Delta]
    total_vanna = _safe_float(user_ctx.total_vanna)
    total_delta = _safe_float(user_ctx.total_weighted_delta)
    capital = _safe_float(user_ctx.capital)

    vanna_impact = total_vanna * (vix_spot * 0.10 / 100.0)
    new_delta = total_delta + vanna_impact

    # 對沖狀態與建議
    hedge_status = "運行中" if abs(total_delta) < (capital * 0.1) else "需調整"
    # 簡單邏輯：如果 Delta 太正，建議賣出 SPY；如果太負，建議買入 SPY
    if total_delta > 100:
        hedge_instruction = f"賣出 {int(total_delta)} 股 SPY 以對沖正 Delta"
    elif total_delta < -100:
        hedge_instruction = f"買入 {int(abs(total_delta))} 股 SPY 以對沖負 Delta"
    else:
        hedge_instruction = "目前曝險平衡，無需立即調整"

    nro_info = (
        f"* **Beta-Delta:** `{total_delta:+.1f}` (相對於 SPY 的整體曝險)\n"
        f"* **Vanna 敏感度:** 若 VIX 上升 10%，隱含 Delta 將變動至 `{new_delta:+.1f}`。\n"
        f"* **對沖狀態:** {hedge_status}\n"
        f"> 🎯 建議對沖位：{hedge_instruction}"
    )
    embed.add_field(name="🛡️ 組合風險精算 (NRO Integrity)", value=nro_info, inline=False)

    # 3. 系統健康
    ram_usage = psutil.virtual_memory().percent
    # 假設 BoundedCache 正常，這裡簡單寫死或從某處獲取
    sys_health = f"RAM {ram_usage}% | BoundedCache 穩定"
    embed.add_field(name="⚙️ 系統健康", value=sys_health, inline=False)

    embed.set_footer(text="Nexus Seeker | 戰術風險管理終端")
    return embed


def create_tactical_symbol_embed(data: Dict[str, Any]) -> discord.Embed:
    """
    建構標的深度分析 (Tactical Deep-Dive) Embed.
    遵循 Task 2 的 Traditional Chinese 模板。
    """
    symbol = data.get("symbol", "UNKNOWN")

    # 處理盤前狀態與波動率 degradation
    iv_data = data.get("iv_data")
    title_suffix = ""
    is_premarket = False
    if iv_data:
        if hasattr(iv_data, "is_premarket"):
            is_premarket = iv_data.is_premarket
        elif isinstance(iv_data, dict):
            is_premarket = iv_data.get("is_premarket", False)

        current_iv_val = (
            iv_data.current_iv
            if hasattr(iv_data, "current_iv")
            else iv_data.get("current_iv", 0.0)
        )
        if is_premarket:
            if current_iv_val > 0.0:
                title_suffix = " [盤前/前日收盤]"
            else:
                title_suffix = " [盤前數據未更新]"

    embed = discord.Embed(
        title=f"🌌 標的分析中心: {symbol}{title_suffix}",
        color=discord.Color.dark_magenta(),
        timestamp=datetime.now(timezone.utc),
    )

    # 1. 💹 即時報價 (Real-time Quote)
    quote = data.get("quote", {})
    c_val = quote.get("c", data.get("price", 0.0))
    dp_val = quote.get("dp", 0.0)
    d_val = quote.get("d", 0.0)
    o_val = quote.get("o", 0.0)
    h_val = quote.get("h", 0.0)
    l_val = quote.get("l", 0.0)
    pc_val = quote.get("pc", 0.0)

    price_emoji = "📈" if dp_val >= 0 else "📉"
    if not quote or c_val == 0.0:
        quote_lines = [
            "```ansi",
            " 當前現價 (Current Price)",
            f" └─ 現價: \u001b[1;37m${c_val:.2f}\u001b[0m (暫無即時報價數據)",
            "```",
        ]
    else:
        color_code = "\u001b[1;32m" if dp_val >= 0 else "\u001b[1;31m"
        quote_lines = [
            "```ansi",
            " 當前現價 (Current Price)",
            f" └─ 現價: {color_code}${c_val:.2f}\u001b[0m ({price_emoji} {color_code}{dp_val:+.2f}%\u001b[0m / {color_code}{d_val:+.2f}\u001b[0m)",
            " 今日區間 (Daily Range)",
            f" └─ 開盤: \u001b[1;36m{o_val:.2f}\u001b[0m | 最高: \u001b[1;31m{h_val:.2f}\u001b[0m | 最低: \u001b[1;32m{l_val:.2f}\u001b[0m | 前收: \u001b[1;30m{pc_val:.2f}\u001b[0m",
            "```",
        ]
    embed.add_field(
        name="💹 即時報價 (Real-time Quote)",
        value="\n".join(quote_lines),
        inline=False,
    )

    # 2. 📐 情緒與邊緣偵測 (Edge Detection)
    skew_val = data.get("skew", 0.0)
    skew_percentile = data.get("skew_percentile", 50.0)
    poly_odds = data.get("polymarket_odds", "N/A")
    reddit_score = data.get("reddit_sentiment_score", "中性")

    divergence = "同步"
    action = "保持觀察"
    if skew_percentile > 80 and (
        "樂觀" in str(reddit_score)
        or "🚀" in str(reddit_score)
        or "Bullish" in str(reddit_score)
    ):
        divergence = "情緒背離 (散戶樂觀 vs 專業避險)"
        action = "建立保護性賣權或減碼"
    elif skew_percentile < 20 and (
        "悲觀" in str(reddit_score)
        or "💀" in str(reddit_score)
        or "Bearish" in str(reddit_score)
    ):
        divergence = "情緒背離 (散戶恐慌 vs 權利金便宜)"
        action = "考慮賣出賣權 (Cash Secured Put)"

    skew_color = "\u001b[1;35m" if skew_percentile > 80 else "\u001b[1;36m"
    sentiment_color = (
        "\u001b[1;32m"
        if "🚀" in str(reddit_score)
        or "樂觀" in str(reddit_score)
        or "Bullish" in str(reddit_score)
        else (
            "\u001b[1;31m"
            if "💀" in str(reddit_score)
            or "悲觀" in str(reddit_score)
            or "Bearish" in str(reddit_score)
            else "\u001b[1;33m"
        )
    )
    divergence_color = "\u001b[1;31m" if divergence != "同步" else "\u001b[1;32m"

    edge_lines = [
        "```ansi",
        " Option Skew (期權偏斜)",
        f" └─ Skew 值: {skew_color}{skew_val:.2f}%\u001b[0m (分位點: {skew_color}{skew_percentile:.1f}%\u001b[0m)",
    ]
    if skew_percentile > 90:
        edge_lines.append(
            "    \u001b[1;33m⚠️ 市場下行保護需求極高，隱含避險情緒升溫。\u001b[0m"
        )

    edge_lines.extend(
        [
            " 巨鯨/散戶意圖映射 (Market Intention)",
            f" ├─ Polymarket 預測勝率: \u001b[1;34m{poly_odds}\u001b[0m",
            f" └─ Reddit 情緒指數: {sentiment_color}{reddit_score}\u001b[0m",
            " 情緒背離偵測 (Divergence Check)",
            f" └─ 狀態: {divergence_color}{divergence}\u001b[0m",
            f" └─ 建議: \u001b[1;32m{action}\u001b[0m",
            "```",
        ]
    )
    embed.add_field(
        name="📐 情緒與邊緣偵測 (Edge Detection)",
        value="\n".join(edge_lines),
        inline=False,
    )

    # 3. 📊 隱含波動率與預期區間 (IV Context)
    if iv_data:
        if hasattr(iv_data, "current_iv"):
            current_iv = iv_data.current_iv
            iv_rank = iv_data.iv_rank
            iv_percentile = iv_data.iv_percentile
            expected_move_weekly = iv_data.expected_move_weekly
            iv_status = iv_data.iv_status
        else:
            current_iv = iv_data.get("current_iv", 0.0)
            iv_rank = iv_data.get("iv_rank", 0.0)
            iv_percentile = iv_data.get("iv_percentile", 0.0)
            expected_move_weekly = iv_data.get("expected_move_weekly", 0.0)
            iv_status = iv_data.get("iv_status", "Normal")

        iv_status_map = {
            "Low": "低 / 便宜",
            "Normal": "正常 / 公允",
            "High": "高 / 昂貴",
            "Extreme": "極高 / 泡沫",
        }
        status_tw = iv_status_map.get(iv_status, "正常 / 公允")

        if is_premarket and current_iv == 0.0:
            iv_lines = [
                "```ansi",
                " Implied Volatility (IV)",
                " └─ 值: \u001b[1;30m--%\u001b[0m (等待開盤 / 盤前未開市)",
                " IV Rank / IV Percentile",
                " └─ IV Rank: \u001b[1;30m--%\u001b[0m | IV Percentile: \u001b[1;30m--%\u001b[0m (狀態: 待開盤)",
                " Expected Move (預期區間)",
                " └─ 本週預期: \u001b[1;30m--\u001b[0m (開盤後更新)",
                "```",
            ]
        elif is_premarket:
            iv_lines = [
                "```ansi",
                " Implied Volatility (IV)",
                f" └─ 值: {current_iv * 100:.1f}% (前日收盤 / 歷史波動率代理)",
                " IV Rank / IV Percentile",
                f" └─ IV Rank: {iv_rank:.1f}% | IV Percentile: {iv_percentile:.1f}% (狀態: {status_tw})",
                " Expected Move (預期區間)",
                f" └─ 本週預期: ±${expected_move_weekly:.2f} (基於前收/HV計算)",
                "```",
            ]
        else:
            iv_lines = [
                "```ansi",
                " Implied Volatility (IV)",
                f" └─ 值: {current_iv * 100:.1f}% (當前 30 天平值期權隱含波動率)",
                " IV Rank / IV Percentile",
                f" └─ IV Rank: {iv_rank:.1f}% | IV Percentile: {iv_percentile:.1f}% (狀態: {status_tw})",
                " Expected Move (預期區間)",
                f" └─ 本週預期: ±${expected_move_weekly:.2f} (基於當前 IV 計算)",
                "```",
            ]
        embed.add_field(
            name="📊 隱含波動率與預期區間 (IV Context)",
            value="\n".join(iv_lines),
            inline=False,
        )

    # 4. 🎯 結算與目標 (Target Lock)
    max_pain = data.get("max_pain", 0.0)
    price = c_val
    distance = ((max_pain - price) / price * 100) if price > 0 else 0.0

    ddp_status = "符合 (符合 DDP 盈餘/估值雙擊)" if data.get("is_ddp") else "不符合"
    ddp_color = "\u001b[1;32m" if data.get("is_ddp") else "\u001b[1;30m"

    ivr = data.get("iv_rank", 0.0)
    ivr_color = "\u001b[1;35m" if ivr > 50 else "\u001b[1;36m"

    pcr_data = data.get("pcr", {})
    pcr_val = pcr_data.get("pcr", 0.0) if pcr_data else 0.0
    pcr_state = pcr_data.get("state", "N/A") if pcr_data else "N/A"
    pcr_color = (
        "\u001b[1;31m"
        if "偏高" in str(pcr_state) or "極高" in str(pcr_state)
        else ("\u001b[1;32m" if "偏低" in str(pcr_state) else "\u001b[1;36m")
    )

    if abs(distance) < 2.0:
        scenario = "價格接近最大痛點，結算日前可能維持震盪。"
        scen_color = "\u001b[1;33m"
    elif distance > 5.0:
        scenario = "價格遠低於最大痛點，具備磁吸效應回升動能。"
        scen_color = "\u001b[1;32m"
    elif distance < -5.0:
        scenario = "價格遠高於最大痛點，需留意結算日前壓回風險。"
        scen_color = "\u001b[1;31m"
    else:
        scenario = "目前價差適中，依技術指標操作為主。"
        scen_color = "\u001b[1;36m"

    dist_color = "\u001b[1;31m" if abs(distance) > 5.0 else "\u001b[1;32m"

    target_lines = [
        "```ansi",
        " 最大痛點結算 (Max Pain Settlement)",
        f" └─ Max Pain價位: \u001b[1;33m${max_pain:.2f}\u001b[0m (當前價差: {dist_color}{distance:+.1f}%\u001b[0m)",
        " DDP 與期權風控 (DDP & Risk Metrics)",
        f" ├─ DDP 估值雙擊: {ddp_color}{ddp_status}\u001b[0m",
        f" ├─ IV Rank: {ivr_color}{ivr:.1f}%\u001b[0m",
        f" └─ Put/Call Ratio: \u001b[1;36m{pcr_val:.2f}\u001b[0m ({pcr_color}{pcr_state}\u001b[0m)",
        " 結算價操作指引 (Scenario Analysis)",
        f" └─ 操作指引: {scen_color}{scenario}\u001b[0m",
        "```",
    ]
    embed.add_field(
        name="🎯 結算與目標 (Target Lock)",
        value="\n".join(target_lines),
        inline=False,
    )

    # 5. 🐋 異常活動 (UOA)
    uoa_data = data.get("uoa", [])
    if uoa_data:
        uoa_lines = ["```ansi"]
        uoa_headers = ["到期日", "履約價", "類型", "機構/OI", "比例"]
        uoa_widths = [10, 7, 4, 15, 6]
        uoa_lines.append(
            " | ".join(
                _pad_string(h, w, "left" if i in (0, 2, 3) else "right")
                for i, (h, w) in enumerate(zip(uoa_headers, uoa_widths))
            )
        )
        uoa_lines.append("-" * (sum(uoa_widths) + 3 * (len(uoa_widths) - 1)))
        for item in uoa_data:
            expiry = str(item.get("expiry", "N/A"))
            strike = f"${item.get('strike', 'N/A')}"
            opt_type = str(item.get("type", "N/A")).upper()
            ratio = f"{item.get('ratio', 'N/A')}x"
            trade_type = str(item.get("trade_type", "SWEEP")).upper()
            oi_change = item.get("oi_change_net", 0)

            tag = "🔥 SWEEP" if trade_type == "SWEEP" else "📦 BLOCK"
            oi_change_str = f"{oi_change:+d}" if oi_change != 0 else "0"
            inst_str = f"{tag}({oi_change_str})"

            expiry = _visual_truncate(expiry, uoa_widths[0])
            strike = _visual_truncate(strike, uoa_widths[1])
            opt_type = _visual_truncate(opt_type, uoa_widths[2])
            inst_str = _visual_truncate(inst_str, uoa_widths[3])
            ratio = _visual_truncate(ratio, uoa_widths[4])

            row_str = " | ".join(
                [
                    _pad_string(expiry, uoa_widths[0], "left"),
                    _pad_string(strike, uoa_widths[1], "right"),
                    _pad_string(opt_type, uoa_widths[2], "left"),
                    _pad_string(inst_str, uoa_widths[3], "left"),
                    _pad_string(ratio, uoa_widths[4], "right"),
                ]
            )
            uoa_lines.append(row_str)
        uoa_lines.append("```")
        embed.add_field(
            name="🐋 異常活動 (UOA)", value="\n".join(uoa_lines), inline=False
        )
    else:
        embed.add_field(
            name="🐋 異常活動 (UOA)",
            value="```ansi\n目前無顯著異常活動\n```",
            inline=False,
        )

    embed.set_footer(
        text="🔗 使用 /settle_hedge 紀錄對沖或 /event_impact 進行曝險模擬。"
    )
    return embed


def create_tactical_hedge_embed(
    symbol: str, ivr: float, rec_strategy: str
) -> discord.Embed:
    """建構標的對沖防禦中心的 Embed"""
    embed = discord.Embed(
        title=f"🛡️ {symbol} 對沖防禦中心 (Tactical Hedging)",
        color=discord.Color.red(),
    )
    embed.add_field(
        name="📊 當前波動率狀態 (Volatility Context)",
        value=f"* **IV Rank:** `{ivr:.1f}%`\n* **推薦防禦策略:** `{rec_strategy}`",
        inline=False,
    )
    embed.add_field(
        name="🛠️ 推薦執行步驟",
        value="1. 請在終端機或聊天室中輸入 `/settle_hedge` 以登錄對沖操作。\n2. 可搭配 `/event_impact` 輸入事件代號模擬近期宏觀事件（財報/CPI）對選擇權曝險的影響。",
        inline=False,
    )
    return embed


def create_watchlist_embed(page_data, current_page, total_pages, total_items):
    """生成觀察清單的分頁 Embed (移除成本欄位)"""

    if not page_data:
        description = "目前沒有追蹤任何項目"
    else:
        lines = ["```ansi"]
        # 1. 標頭修改為兩欄
        header = (
            f"{_pad_string('標的', 12)} | {_pad_string('AI 分析 (LLM)', 12, 'right')}"
        )
        lines.append(header)

        # 2. 分隔線
        lines.append("-" * 27)

        for sym, use_llm in page_data:
            sym_fmt = _pad_string(sym, 12)
            llm_text = "開啟 (ON)" if use_llm else "關閉 (OFF)"
            llm_fmt = _pad_string(llm_text, 12, "right")
            lines.append(f"{sym_fmt} | {llm_fmt}")

        lines.append("```")
        description = "\n".join(lines)

    embed = discord.Embed(
        title="📡 【您的專屬觀察清單】",
        description=f"目前監控中的標的清單。系統將每 30 分鐘自動執行量化掃描。\n\n{description}",
        color=discord.Color.blurple(),
    )

    embed.set_footer(
        text=f"頁次: {current_page}/{total_pages} ｜ 📊 總項目: {total_items}"
    )
    return embed


def create_watchlist_signal_embed(
    symbol: str,
    report_body: str,
    option_guidance: str,
    event_risk_summary: str,
    skew_state: str,
    alert_level: str,
    option_plan: WatchlistOptionPlan | None = None,
    skew_commentary: str | None = None,
    has_position: bool = False,
    holding_quantity: float | None = None,
    holding_avg_cost: float | None = None,
    holding_pnl_pct: float | None = None,
    suitable_buy_price: float | None = None,
    suitable_buy_shares: int | None = None,
    suitable_sell_price: float | None = None,
    suitable_sell_shares: int | None = None,
    buy_rationale: str | None = None,
    sell_rationale: str | None = None,
) -> discord.Embed:
    """建立 watchlist 半小時心跳推播 Embed (視覺排版與情緒掃描一致)。"""
    color = {
        "red": discord.Color.red(),
        "yellow": discord.Color.orange(),
        "green": discord.Color.green(),
    }.get(alert_level, discord.Color.blurple())
    level_text = {
        "red": "🔴 高優先",
        "yellow": "🟡 注意觀察",
        "green": "🟢 例行追蹤",
    }.get(alert_level, "🔵 一般")

    embed = discord.Embed(
        title=f"📡 Watchlist 半小時戰報：{symbol}",
        description=(
            f"**警報等級：** {level_text}\n"
            "根據價位 / 技術面、期權結構與 Skew 的半小時盤中快照。"
        ),
        color=color,
        timestamp=datetime.now(timezone.utc),
    )
    # Split the report body into three sections if it contains the standard headers
    section_price = ""
    section_defense = ""
    section_sddm = ""

    if (
        "技術面與現價快照" in report_body
        and "🛡️ 技術 / 防禦牆" in report_body
        and "⚙️ SDDM / 對沖" in report_body
    ):
        lines = report_body.strip().split("\n")
        clean_lines = []
        for line in lines:
            if line.strip() in ("```ansi", "```"):
                continue
            clean_lines.append(line)

        sec1_lines = []
        sec2_lines = []
        sec3_lines = []

        current_sec = 1
        for line in clean_lines:
            if "🛡️ 技術 / 防禦牆" in line:
                current_sec = 2
            elif "⚙️ SDDM / 對沖" in line:
                current_sec = 3

            # Skip the divider lines so that the three separate code blocks are clean
            if "----------------------------------" in line:
                continue

            if current_sec == 1:
                sec1_lines.append(line)
            elif current_sec == 2:
                sec2_lines.append(line)
            elif current_sec == 3:
                sec3_lines.append(line)

        if sec1_lines:
            section_price = "```ansi\n" + "\n".join(sec1_lines) + "\n```"
        if sec2_lines:
            section_defense = "```ansi\n" + "\n".join(sec2_lines) + "\n```"
        if sec3_lines:
            section_sddm = "```ansi\n" + "\n".join(sec3_lines) + "\n```"
    else:
        # Fallback to displaying all of it in the first field, and empty/placeholder in the others
        section_price = report_body
        section_defense = "```ansi\n 🛡️ 暫無防禦牆快照數據\n```"
        section_sddm = "```ansi\n ⚙️ 暫無 SDDM / 對沖數據\n```"

    embed.add_field(
        name="📊 技術與現價快照",
        value=_safe_embed_field_value(section_price, "暫無快照"),
        inline=False,
    )
    embed.add_field(
        name="🛡️ 技術 / 防禦牆",
        value=_safe_embed_field_value(section_defense, "暫無防禦牆"),
        inline=False,
    )
    embed.add_field(
        name="⚙️ SDDM / 對沖",
        value=_safe_embed_field_value(section_sddm, "暫無對沖機制"),
        inline=False,
    )

    skew_lines = ["```ansi"]
    skew_lines.append(f" 📐 {symbol} 期權偏斜判讀 (Option Skew Scan)")
    skew_lines.append(" ----------------------------------")
    skew_lines.append(f" └─ Skew 數據: \u001b[1;36m{skew_state}\u001b[0m")
    if option_plan is not None:
        skew_lines.append(f" └─ 策略解說: \u001b[3m{option_plan.rationale}\u001b[0m")
    else:
        skew_lines.append(" └─ 策略解說: 目前無可執行期權合約計畫")
    skew_lines.append("```")
    embed.add_field(
        name="📐 Skew 與市場判讀",
        value=_safe_embed_field_value("\n".join(skew_lines), "N/A"),
        inline=False,
    )

    if skew_commentary:
        commentary_lines = ["```ansi"]
        commentary_lines.append(" ⚡ Skew 即時智能診斷 (Rule Engine)")
        commentary_lines.append(" ----------------------------------")
        wrapped_lines = _wrap_visual(skew_commentary.strip(), width=45, indent="   ")
        for line in wrapped_lines:
            commentary_lines.append(f" └─ {line}")
        commentary_lines.append("```")
        commentary_text = "\n".join(commentary_lines)
    else:
        commentary_text = "```ansi\n ⚡ 暫無 Skew 即時智能診斷\n```"
    embed.add_field(
        name="⚡ Skew 即時智能診斷",
        value=_safe_embed_field_value(commentary_text, "暫無解說"),
        inline=False,
    )

    event_lines = ["```ansi"]
    event_lines.append(" 🗓️ 近期重大事件 (Macro & Earnings Events)")
    event_lines.append(" ----------------------------------")
    if event_risk_summary:
        wrapped_event = _wrap_visual(event_risk_summary.strip(), width=45, indent="   ")
        for line in wrapped_event:
            event_lines.append(f" └─ {line}")
    else:
        event_lines.append(" └─ 未偵測到近期重大事件")
    event_lines.append("```")
    event_val = "\n".join(event_lines)
    embed.add_field(
        name="🗓️ 事件風控",
        value=_safe_embed_field_value(event_val, "未偵測到近期重大事件"),
        inline=False,
    )

    holding_lines = ["```ansi"]
    holding_lines.append(f" 💼 {symbol} 持倉與操作指引 (Holding & Trading Guide)")
    holding_lines.append(" ----------------------------------")
    if has_position:
        holding_lines.append(" └─ 部位狀態: \u001b[1;32m已持有\u001b[0m")
        if holding_quantity is not None:
            quantity_text = f"{holding_quantity:,.2f}".rstrip("0").rstrip(".")
            holding_lines.append(f" └─ 現貨股數: {quantity_text} 股")
            if holding_avg_cost is not None and holding_avg_cost > 0.0:
                holding_lines.append(f" └─ 平均成本: ${holding_avg_cost:,.2f}")
                if holding_pnl_pct is not None:
                    pnl_val = holding_pnl_pct * 100
                    pnl_color = (
                        "\u001b[1;32m"
                        if pnl_val > 0
                        else "\u001b[1;31m"
                        if pnl_val < 0
                        else "\u001b[0m"
                    )
                    pnl_icon = "🟢" if pnl_val > 0 else "🔴" if pnl_val < 0 else "⚪"
                    holding_lines.append(
                        f" └─ 標的損益: {pnl_icon} {pnl_color}{pnl_val:+.2f}%\u001b[0m"
                    )
        if suitable_sell_price is not None and suitable_sell_price > 0.0:
            holding_lines.append(
                f" └─ 適合賣出價位: \u001b[1;33m${suitable_sell_price:,.2f}\u001b[0m (基於 Skew 與阻力位微調)"
            )
            if suitable_sell_shares is not None and suitable_sell_shares > 0:
                holding_lines.append(
                    f" └─ 建議賣出股數: \u001b[1;35m{suitable_sell_shares}\u001b[0m 股"
                )
            if sell_rationale:
                holding_lines.append(f" └─ 操盤減碼指引: {sell_rationale}")
    else:
        holding_lines.append(" └─ 部位狀態: 未持有")
        if suitable_buy_price is not None and suitable_buy_price > 0.0:
            holding_lines.append(
                f" └─ 適合買入價位: \u001b[1;32m${suitable_buy_price:,.2f}\u001b[0m (基於 Skew 折價調整)"
            )
            if suitable_buy_shares is not None and suitable_buy_shares > 0:
                holding_lines.append(
                    f" └─ 建議買入股數: \u001b[1;36m{suitable_buy_shares}\u001b[0m 股"
                )
            if buy_rationale:
                holding_lines.append(f" └─ 操盤進場指引: {buy_rationale}")
        else:
            holding_lines.append(" └─ 操作提示: 目前無持倉，以 watchlist 追蹤觀察為主")
    holding_lines.append("```")
    embed.add_field(
        name="💼 持倉與操作指引",
        value=_safe_embed_field_value("\n".join(holding_lines), "暫無持倉與操作指引"),
        inline=False,
    )

    guidance_lines = ["```ansi"]
    guidance_lines.append(" 🎯 交易執行建議 (Tactical Option Guidance)")
    guidance_lines.append(" ----------------------------------")
    if option_guidance:
        wrapped_guidance = _wrap_visual(option_guidance.strip(), width=45, indent="   ")
        for line in wrapped_guidance:
            guidance_lines.append(f" └─ {line}")
    else:
        guidance_lines.append(" └─ 暫無執行建議")
    guidance_lines.append("```")
    guidance_text = "\n".join(guidance_lines)
    embed.add_field(
        name="🎯 執行建議",
        value=_safe_embed_field_value(guidance_text, "暫無建議"),
        inline=False,
    )

    if option_plan is not None:
        is_covered_call = "Covered Call" in option_plan.strategy_name
        premium_type_tw = (
            "收入 / Credit"
            if is_covered_call
            else (
                "Debit / 收支"
                if option_plan.premium_type == "debit"
                else "Credit / 收租"
            )
        )
        plan_lines = ["```ansi"]
        plan_lines.append(" 🧾 建議期權合約 (Suggested Option Contract)")
        plan_lines.append(" ----------------------------------")
        plan_lines.append(
            f" ├─ 策略名稱: \u001b[1;36m{option_plan.strategy_name}\u001b[0m ({premium_type_tw})"
        )

        if is_covered_call:
            plan_lines.append(
                f" ├─ 預估權利金: \u001b[1;32m${option_plan.estimated_net_premium:.2f}\u001b[0m ({premium_type_tw})"
            )
            if option_plan.legs:
                leg = option_plan.legs[0]
                plan_lines.append(
                    f" └─ 執行合約結構: {leg.action.upper()} {leg.opt_type.upper()} {leg.strike:.2f} {leg.expiry} @ {leg.mid_price:.2f} (鎖定 Max Pain 利益中樞)"
                )
        else:
            plan_lines.append(
                f" ├─ 預估權利金: \u001b[1;32m${option_plan.estimated_net_premium:.2f}\u001b[0m (建議 \u001b[1;35m{option_plan.suggested_contracts}\u001b[0m 口)"
            )
            plan_lines.append(
                f" ├─ 估計最大風險: \u001b[1;31m${option_plan.max_risk_amount:.2f}\u001b[0m"
            )
            plan_lines.append(" └─ 執行合約結構:")
            for i, leg in enumerate(option_plan.legs):
                is_last = i == len(option_plan.legs) - 1
                connector = "    └──" if is_last else "    ├──"
                leg_str = f"{connector} {leg.action.upper()} {leg.opt_type.upper()} {leg.strike:.2f} {leg.expiry} @ {leg.mid_price:.2f}"
                plan_lines.append(leg_str)
        plan_lines.append("```")
        option_value = "\n".join(plan_lines)
    else:
        option_value = (
            "```ansi\n"
            " 🧾 建議期權合約 (Suggested Option Contract)\n"
            " ----------------------------------\n"
            " └─ 目前無符合條件的完整期權合約，保留現貨/策略觀察。\n"
            "```"
        )
    embed.add_field(
        name="🧾 可執行期權合約",
        value=_safe_embed_field_value(option_value, "暫無合約"),
        inline=False,
    )
    embed.set_footer(text="Nexus Seeker Watchlist Heartbeat | 每 30 分鐘更新")
    return embed


def create_watchlist_overview_embed(
    summary_items: List[Dict[str, str]],
    llm_overview: str | None = None,
) -> discord.Embed:
    """建立單一使用者本輪 watchlist 總覽摘要 Embed。"""
    scenario_labels = {
        "hard-hedge": "防守對沖",
        "premium-harvest": "權利金佈局",
        "wait": "觀望待機",
    }
    priority = {"red": 0, "yellow": 1, "green": 2}
    icon_map = {"red": "🔴", "yellow": "🟡", "green": "🟢"}
    ordered_items = sorted(
        summary_items,
        key=lambda item: (
            priority.get(item.get("alert_level", ""), 3),
            item.get("symbol", ""),
        ),
    )
    counts = {
        level: sum(1 for item in ordered_items if item.get("alert_level") == level)
        for level in ("red", "yellow", "green")
    }
    embed_color = (
        discord.Color.red()
        if counts["red"] > 0
        else discord.Color.orange()
        if counts["yellow"] > 0
        else discord.Color.green()
    )

    embed = discord.Embed(
        title="🧭 本輪 Watchlist 總覽",
        description=(
            f"**追蹤標的：** `{len(ordered_items)}` ｜ "
            f"🔴 `{counts['red']}` ｜ 🟡 `{counts['yellow']}` ｜ 🟢 `{counts['green']}`\n"
            "先看高優先標的，再回頭逐則檢查個別 heartbeat。"
        ),
        color=embed_color,
        timestamp=datetime.now(timezone.utc),
    )

    focus_lines = []
    for item in ordered_items[:3]:
        icon = icon_map.get(item.get("alert_level", ""), "🔵")
        scenario_label = scenario_labels.get(
            item.get("scenario", ""),
            item.get("scenario", "觀望待機"),
        )
        pnl_suffix = ""
        if "holding_pnl_pct" in item and item["holding_pnl_pct"] is not None:
            pnl_val = float(item["holding_pnl_pct"]) * 100
            pnl_icon = "🟢" if pnl_val > 0 else "🔴" if pnl_val < 0 else "⚪"
            pnl_suffix = f" ｜ {pnl_icon} 現貨損益: `{pnl_val:+.2f}%`"
        focus_lines.append(
            f"{icon} {item.get('symbol', 'N/A')}｜{item.get('skew_state', 'N/A')}｜{scenario_label}{pnl_suffix}"
        )
        focus_lines.append(
            f"事件：{item.get('event_risk_summary', '未偵測到近期重大事件')}"
        )
    if not focus_lines:
        focus_lines = ["本輪無可用 watchlist 評估結果。"]
    embed.add_field(
        name="🎯 本輪焦點",
        value=_safe_embed_field_value("\n".join(focus_lines), "暫無重點"),
        inline=False,
    )

    overview_lines = []
    for item in ordered_items:
        icon = icon_map.get(item.get("alert_level", ""), "🔵")
        scenario_label = scenario_labels.get(
            item.get("scenario", ""),
            item.get("scenario", "觀望待機"),
        )
        pnl_suffix = ""
        if "holding_pnl_pct" in item and item["holding_pnl_pct"] is not None:
            pnl_val = float(item["holding_pnl_pct"]) * 100
            pnl_icon = "🟢" if pnl_val > 0 else "🔴" if pnl_val < 0 else "⚪"
            pnl_suffix = f" ｜ {pnl_icon} 現貨損益: `{pnl_val:+.2f}%`"
        overview_lines.append(
            f"{icon} {item.get('symbol', 'N/A')}｜{item.get('skew_state', 'N/A')}｜{scenario_label}{pnl_suffix}"
        )
    embed.add_field(
        name="📋 全標的速覽",
        value=_safe_embed_field_value("\n".join(overview_lines), "暫無總覽"),
        inline=False,
    )
    embed.add_field(
        name="🤖 LLM 本輪摘要",
        value=_safe_embed_field_value(
            llm_overview or "",
            "暫無本輪 LLM 摘要，請優先查看紅 / 黃燈標的與事件風控。",
        ),
        inline=False,
    )
    embed.set_footer(text="Nexus Seeker Watchlist Roundup | 每 30 分鐘更新")
    return embed


def create_portfolio_report_embed(
    report_lines, hedge_analysis=None, survival_runway=None
):
    """
    將 check_portfolio_status_logic 產出的 report_lines 轉換為漂亮的 Discord Embed
    """
    # 處理完全為空的狀況
    if not report_lines:
        embed = discord.Embed(
            title="📊 Nexus Seeker 盤後風險結算報告",
            description="目前無持倉部位，亦無風險數據。\n\u200b",
            color=discord.Color.blue(),
            timestamp=datetime.now(timezone.utc),
        )
        embed.set_footer(text="Argo Risk Engine v2.6 | 基準標的: SPY")
        return embed

    # 1. 分割資料：將個別持倉與宏觀報告分開
    # 尋找分割點：🌐 【宏觀風險與資金水位報告】
    macro_index = -1
    for i, line in enumerate(report_lines):
        if _is_macro_report_marker(line):
            macro_index = i
            break

    # 2. 處理持倉細節與宏觀報告區隔
    if macro_index != -1:
        positions_list = [
            line.strip() for line in report_lines[:macro_index] if line.strip()
        ]
        macro_text = "\n".join(
            line.strip() for line in report_lines[macro_index:] if line.strip()
        )
    else:
        # 如果找不到宏觀報告區塊，將所有內容視為持倉明細
        positions_list = [line.strip() for line in report_lines if line.strip()]
        macro_text = "目前無宏觀風險數據。"

    # 使用 \n\n 分隔部位
    if positions_list:
        positions_text = _parse_and_format_positions_table(
            positions_list, survival_runway
        )
    else:
        positions_text = "目前無持倉部位。"

    # 二次確認，防止 macro_text 全空或只包含空白，Discord Embed value 必須為非空字串
    if not macro_text:
        macro_text = "無宏觀風險數據。"

    # 4. 判斷顏色：如果有任何 "🚨" 或 "🆘"，就用紅色，否則用藍色
    embed_color = discord.Color.blue()
    if "🚨" in macro_text or "🆘" in macro_text:
        embed_color = discord.Color.red()
    elif "⚠️" in macro_text:
        embed_color = discord.Color.orange()

    embed = discord.Embed(
        title="📊 Nexus Seeker 盤後風險結算報告",
        color=embed_color,
        timestamp=datetime.now(timezone.utc),
    )

    # 🚀 [Professional Investor] 財務生存跑道 (Priority Header)
    if survival_runway is not None:
        runway_text = (
            "無限 (收益已覆蓋支出)"
            if survival_runway >= 9999
            else f"`{survival_runway:,.1f}` 天"
        )
        embed.add_field(
            name="🏁 財務生存跑道 (Financial Runway)",
            value=f"```yaml\n預估剩餘天數: {runway_text}\n(基於現有現金儲備與 Theta 收益)\n```\n\u200b",
            inline=False,
        )

    # 🚀 欄位一：個別持倉細節
    if (
        positions_list
        and "positions_text" in locals()
        and positions_text != "目前無持倉部位。"
    ):
        # 如果包含財務摘要，將其分離出來單獨處理
        if "財務摘要 (Financial Summary)" in positions_text:
            table_part, summary_part = positions_text.split(
                "\n\n財務摘要 (Financial Summary)"
            )
            summary_text = "\n\n財務摘要 (Financial Summary)" + summary_part
            if summary_text.endswith("\n```"):
                summary_text = summary_text[:-4]  # 移除字尾的 ```
        else:
            table_part = positions_text
            summary_text = ""

        raw_lines = table_part.split("\n")
        if raw_lines and raw_lines[-1] == "```":
            raw_lines = raw_lines[:-1]
        if len(raw_lines) >= 3 and raw_lines[0] == "```ansi":
            header = raw_lines[1]
            divider = raw_lines[2]
            data_lines = raw_lines[3:]
            chunks = _chunk_ansi_table(header, divider, data_lines)

            # 將財務摘要追加到最後一個 chunk 中
            if summary_text:
                if chunks:
                    last_chunk = chunks[-1]
                    if last_chunk.endswith("```"):
                        last_chunk = last_chunk[:-3] + summary_text + "\n```"
                        chunks[-1] = last_chunk
                    else:
                        chunks[-1] = last_chunk + "\n" + summary_text

            for i, chunk in enumerate(chunks):
                name = (
                    f"📦 當前持倉明細 ({i+1}/{len(chunks)})"
                    if len(chunks) > 1
                    else "📦 當前持倉明細"
                )
                embed.add_field(name=name, value=chunk, inline=False)
        else:
            positions_text = _safe_embed_field_value(positions_text, "目前無持倉部位。")
            embed.add_field(name="📦 當前持倉明細", value=positions_text, inline=False)
    else:
        embed.add_field(name="📦 當前持倉明細", value="目前無持倉部位。", inline=False)

    # 🚀 欄位二：全帳戶宏觀風險與對沖指令 (核心！)
    macro_text = _safe_embed_field_value(macro_text, "無宏觀風險數據。")
    embed.add_field(name="🛡️ 風控管線評估與對沖決策", value=macro_text, inline=False)

    # 🚀 欄位三：對沖有效性分析 (新增)
    if hedge_analysis:
        build_hedge_analysis_field(embed, hedge_analysis)

        # 如果有 Tau 係數更新資訊，則額外提示
        dynamic_tau = getattr(embed, "dynamic_tau", None) or (
            hedge_analysis.get("dynamic_tau")
            if isinstance(hedge_analysis, dict)
            else None
        )
        if dynamic_tau is not None:
            dynamic_tau = _safe_float(dynamic_tau, 1.0)
            embed.add_field(
                name="🧬 STHE 自動優化狀態",
                value=f"目前對沖調教因子 τ: `{dynamic_tau:.2f}`",
                inline=False,
            )

    embed.set_footer(text="Argo Risk Engine v2.6 | 基準標的: SPY")

    return embed


def create_transition_suggestion_embed(data: Dict[str, Any]) -> discord.Embed:
    """建構倉位演進建議 (Transition Suggestion) 的 Discord Embed"""
    sym = data["symbol"]
    pnl_pct = data["pnl_pct"] * 100
    pnl_usd = data["pnl_usd"]
    res = data["transition_result"]
    stock_p = data["stock_price"]

    embed = discord.Embed(
        title=f"🔄 倉位演進建議 | {sym}",
        description="偵測到投機部位獲利豐厚，建議轉向「收租模式」以鎖定長期收益。",
        color=discord.Color.gold(),
        timestamp=datetime.now(timezone.utc),
    )

    # 目前獲利狀況
    embed.add_field(
        name="📈 目前獲利狀況",
        value=f"獲利率: `{pnl_pct:+.1f}%` / `${pnl_usd:,.2f}`\n\u200b",
        inline=True,
    )

    # 演進後效益
    embed.add_field(
        name="🧬 演進後預期效益",
        value=(
            f"調整後持股成本: **`${res.adjusted_cost_basis:.2f}`**\n"
            f"相對現價折讓: `{res.capital_efficiency_gain:.1f}%` (現價: `${stock_p:.2f}`)\n"
            f"預期 CC 年化報酬 (AROC): `{res.projected_aroc:.1f}%` (@${res.cc_strike})\n\u200b"
        ),
        inline=False,
    )

    # 操作指令建議
    embed.add_field(
        name="🛠️ 建議操作路徑",
        value=(
            f"1. **Synthetic Exit**: 獲利平倉目前 Option 部位。\n"
            f"2. **Core Equity**: 使用獲利抵扣，購入 100 股現股。\n"
            f"3. **Covered Call**: 賣出 `${res.cc_strike}` Call 啟動收租。\n\u200b"
        ),
        inline=False,
    )

    embed.set_footer(text="Nexus Position Evolution Engine | 專業投資者建議")
    return embed


def build_vtr_stats_embed(
    user_name: str, stats: dict, attribution_lines: Optional[List[str]] = None
) -> discord.Embed:
    """
    建構 VTR 績效統計 Embed 面板，含對沖效能歸因。
    """
    # 根據勝率決定顏色
    win_rate = stats.get("win_rate", 0)
    if win_rate >= 60:
        color = 0x2ECC71  # 綠色 (Success)
        status_icon = "🟢"
    elif win_rate >= 40:
        color = 0xF1C40F  # 黃色 (Warning)
        status_icon = "🟡"
    else:
        color = 0xE74C3C  # 紅色 (Danger)
        status_icon = "🔴"

    embed = discord.Embed(
        title="📈 Nexus Seeker | 虛擬交易室 (VTR) 績效總結",
        description=f"{status_icon} 使用者: **{user_name}** 的系統歸因分析",
        color=color,
        timestamp=datetime.now(timezone.utc),
    )

    # 績效統計指標 table
    pnl = stats.get("total_pnl", 0.0)
    pnl_str = f"+${pnl:,.2f}" if pnl >= 0 else f"-${abs(pnl):,.2f}"
    avg_pnl = stats.get("avg_pnl", 0.0)
    avg_pnl_str = f"+${avg_pnl:.2f}" if avg_pnl >= 0 else f"-${abs(avg_pnl):.2f}"

    if win_rate >= 60:
        win_color = " \033[0;32m"
    elif win_rate >= 40:
        win_color = " \033[0;33m"
    else:
        win_color = " \033[0;31m"

    pnl_color = " \033[0;32m" if pnl >= 0 else " \033[0;31m"
    avg_pnl_color = " \033[0;32m" if avg_pnl >= 0 else " \033[0;31m"

    vtr_lines = ["```ansi"]
    headers = ["績效指標", "數據值"]
    widths = [14, 18]
    vtr_lines.append(" | ".join(_pad_string(h, w) for h, w in zip(headers, widths)))
    vtr_lines.append("-" * (sum(widths) + 3))

    vtr_lines.append(
        f"{_pad_string('總結算次數', widths[0])} | {_pad_string(str(stats.get('total_trades', 0)), widths[1])}"
    )

    win_rate_val = f"{win_rate}%"
    vtr_lines.append(
        f"{_pad_string('勝率', widths[0])} | {_pad_string(win_rate_val, widths[1]).replace(win_rate_val, f'{win_color}{win_rate_val}\033[0m')}"
    )

    vtr_lines.append(
        f"{_pad_string('累計總損益', widths[0])} | {_pad_string(pnl_str, widths[1]).replace(pnl_str, f'{pnl_color}{pnl_str}\033[0m')}"
    )

    vtr_lines.append(
        f"{_pad_string('平均單筆損益', widths[0])} | {_pad_string(avg_pnl_str, widths[1]).replace(avg_pnl_str, f'{avg_pnl_color}{avg_pnl_str}\033[0m')}"
    )
    vtr_lines.append("```")

    embed.add_field(name="📊 VTR 績效指標", value="\n".join(vtr_lines), inline=False)

    # 對沖歸因報告
    if attribution_lines:
        attr_text = "\n".join(attribution_lines)
        # 截斷過長內容
        if len(attr_text) > 1024:
            attr_text = attr_text[:1021] + "..."
        embed.add_field(name="🛡️ 對沖效能與自我進化", value=attr_text, inline=False)

    embed.set_footer(text="Nexus Sandbox Engine | 數據包含已平倉之虛擬部位")

    return embed


def build_scan_report(result: Dict[str, Any]):
    """
    量化掃描報告Embed，整合 Greeks, NRO 與 EMA 訊號。
    """
    ai_decision = result.get("ai_decision", "SKIP")
    color = (
        0x2ECC71
        if ai_decision == "APPROVE"
        else (0xE74C3C if ai_decision == "VETO" else 0x3498DB)
    )

    embed = discord.Embed(
        title=f"📡 量化掃描報告: {result['symbol']}",
        description=f"策略: `{result.get('strategy', 'N/A')}` | 履約價: `${result.get('strike', 'N/A')}` | 到期日: `{result.get('target_date', 'N/A')}`",
        color=color,
    )

    # 1. Greeks 區塊 (從 result 中提取)
    g_delta = result.get("delta", 0.0)
    g_theta = result.get("theta", 0.0)
    g_gamma = result.get("gamma", 0.0)
    g_iv = result.get("iv", 0.0)

    greeks_lines = ["```ansi"]
    g_headers = ["希臘字母", "數據值"]
    g_widths = [14, 18]
    greeks_lines.append(
        " | ".join(_pad_string(h, w) for h, w in zip(g_headers, g_widths))
    )
    greeks_lines.append("-" * (sum(g_widths) + 3))

    greeks_lines.append(
        f"{_pad_string('Delta', g_widths[0])} | {_pad_string(f'{g_delta:+.3f}', g_widths[1])}"
    )
    greeks_lines.append(
        f"{_pad_string('Theta', g_widths[0])} | {_pad_string(f'{g_theta:+.4f}', g_widths[1])}"
    )
    greeks_lines.append(
        f"{_pad_string('Gamma', g_widths[0])} | {_pad_string(f'{g_gamma:+.6f}', g_widths[1])}"
    )
    greeks_lines.append(
        f"{_pad_string('IV (隱含波動率)', g_widths[0])} | {_pad_string(f'{g_iv:.1%}', g_widths[1])}"
    )
    greeks_lines.append("```")
    greeks_info = "\n".join(greeks_lines)
    embed.add_field(name="🧬 Greeks 希臘字母", value=greeks_info, inline=False)

    # 2. NRO 風控區塊
    safe_qty = result.get("safe_qty", 0)
    projected = result.get("projected_exposure_pct", 0.0)
    risk_limit = result.get("risk_limit", 15.0)

    proj_color = " \033[0;31m" if projected > risk_limit else " \033[0;32m"
    proj_str = f"{projected:+.1f}%"

    nro_lines = ["```ansi"]
    n_headers = ["風控項目", "數據值"]
    n_widths = [14, 18]
    nro_lines.append(" | ".join(_pad_string(h, w) for h, w in zip(n_headers, n_widths)))
    nro_lines.append("-" * (sum(n_widths) + 3))

    nro_lines.append(
        f"{_pad_string('建議口數', n_widths[0])} | {_pad_string(f'{safe_qty} 口', n_widths[1])}"
    )
    nro_lines.append(
        f"{_pad_string('預期總曝險', n_widths[0])} | {_pad_string(proj_str, n_widths[1]).replace(proj_str, f'{proj_color}{proj_str}\033[0m')}"
    )
    nro_lines.append(
        f"{_pad_string('曝險風控紅線', n_widths[0])} | {_pad_string(f'{risk_limit:.1f}%', n_widths[1])}"
    )
    nro_lines.append("```")
    nro_info = "\n".join(nro_lines)
    embed.add_field(name="🛡️ NRO 風控判定", value=nro_info, inline=False)

    # 3. 🚀 整合 EMA 訊號區塊
    ema_ui = get_ema_signal_ui(result.get("ema_signals", []))
    embed.add_field(name="📈 趨勢與指標動態", value=ema_ui, inline=False)

    # 4. 加上宏觀背景燈號 (VIX/Oil)
    vix = result.get("macro_vix", result.get("vix", 0))
    oil = result.get("macro_oil", result.get("oil", 0))
    vix_status = "🔴" if vix > 25 else "🟢"

    embed.set_footer(
        text=f"環境感知: VIX {vix} {vix_status} | WTI ${oil} | 基準 SPY: ${result.get('spy_price', 0):.1f}"
    )

    return embed


def create_rehedge_embed(rehedge_info: Dict[str, Any]) -> discord.Embed:
    """
    建構「自動避險回補建議」的 Discord Embed 面板。
    """
    priority = rehedge_info.get("priority", "NORMAL")
    color = 0xF1C40F if priority == "NORMAL" else 0xE74C3C  # 黃色或紅色

    symbol = rehedge_info.get("symbol", "SPY")
    suggested_qty = rehedge_info.get("suggested_spy_qty", 0)
    reason = rehedge_info.get("reason", "偵測到曝險異常或市場轉弱")

    embed = discord.Embed(
        title="🛡️ 防禦啟動：自動避險回補建議",
        color=color,
        description=f"標的: **{symbol}**",
    )

    embed.add_field(name="觸發原因", value=f"```\n{reason}\n```", inline=False)

    action_val = (
        f"賣出 (Short) `{suggested_qty}` 股 SPY"
        if suggested_qty > 0
        else f"買入 (Long) `{abs(suggested_qty)}` 股 SPY"
    )
    embed.add_field(name="建議動作", value=action_val, inline=True)

    embed.set_footer(text="提示：當前趨勢已走弱，掛回避險可鎖定現有獲利。")
    embed.timestamp = datetime.now(timezone.utc)

    return embed


def create_ddp_embed(report: Dict[str, Any]) -> discord.Embed:
    """建構 Davis Double Play (DDP) 預警 Embed"""
    sym = report["symbol"]
    curr_pe = report["current_pe"]
    pe_mean = report["pe_mean_3y"]
    eps_growth = report["eps_growth"] * 100
    rev_accel = "加速 (Accelerating)" if report["rev_accel"] else "穩定 (Stable)"
    score = report["confidence_score"]

    # 計算 P/E 均值回歸空間 (預期漲幅)
    pe_upside = (pe_mean / curr_pe - 1) * 100 if curr_pe > 0 else 0

    embed = discord.Embed(
        title=f"🌌 Nexus 戴維斯雙擊預警: {sym}",
        description="偵測到標的符合 **Davis Double Play (DDP)** 條件：盈餘增長與估值擴張的雙重共振。",
        color=0x00FF7F,  # SpringGreen
        timestamp=datetime.now(timezone.utc),
    )

    ddp_lines = ["```ansi"]
    headers = ["DDP 量化指標", "數據值"]
    widths = [24, 20]
    ddp_lines.append(" | ".join(_pad_string(h, w) for h, w in zip(headers, widths)))
    ddp_lines.append("-" * (sum(widths) + 3))

    ddp_lines.append(
        f"{_pad_string('目前本益比 (TTM P/E)', widths[0])} | {_pad_string(f'{curr_pe:.2f}', widths[1])}"
    )
    ddp_lines.append(
        f"{_pad_string('3 年本益比均值 (3Y Mean)', widths[0])} | {_pad_string(f'{pe_mean:.2f}', widths[1])}"
    )

    upside_str = f"+{pe_upside:.1f}%" if pe_upside >= 0 else f"{pe_upside:.1f}%"
    ddp_lines.append(
        f"{_pad_string('本益比估值回歸空間', widths[0])} | {_pad_string(upside_str, widths[1]).replace(upside_str, f'\033[0;32m{upside_str}\033[0m' if pe_upside >= 0 else f'\033[0;31m{upside_str}\033[0m')}"
    )

    growth_str = f"{eps_growth:+.1f}%"
    ddp_lines.append(
        f"{_pad_string('預估 EPS 成長率', widths[0])} | {_pad_string(growth_str, widths[1]).replace(growth_str, f'\033[0;32m{growth_str}\033[0m' if eps_growth >= 0 else f'\033[0;31m{growth_str}\033[0m')}"
    )

    ddp_lines.append(
        f"{_pad_string('營收加速狀態', widths[0])} | {_pad_string(rev_accel, widths[1])}"
    )

    score_str = f"{score:.0f}/100"
    ddp_lines.append(
        f"{_pad_string('DDP 信心評分', widths[0])} | {_pad_string(score_str, widths[1]).replace(score_str, f'\033[0;32m{score_str}\033[0m' if score >= 70 else (f'\033[0;33m{score_str}\033[0m' if score >= 50 else f'\033[0;31m{score_str}\033[0m'))}"
    )
    ddp_lines.append("```")

    embed.add_field(name="📊 DDP 量化指標", value="\n".join(ddp_lines), inline=False)

    # 邏輯解釋
    logic_text = (
        f"1. **盈餘動能**: YoY 成長率 `{eps_growth:.1f}%` 超過 15% 門檻。\n"
        f"2. **估值壓縮**: 目前 P/E 處於 3 年歷史低位 (25% 分位數以下)。\n"
        f"3. **前瞻預期**: Forward P/E `{report['forward_pe']:.2f}` 低於目前 TTM P/E。\n"
        f"4. **動能確認**: 營收成長較前一週期加速。"
    )
    embed.add_field(name="🧐 量化篩選邏輯", value=logic_text, inline=False)

    embed.set_footer(text="Nexus Quantitative Research | 戴維斯雙擊引擎 v1.0")
    return embed


def create_volatility_embed(report: Dict[str, Any]) -> discord.Embed:
    """建構波動率優勢偵測 (Cheap Volatility) 預警 Embed"""
    sym = report["symbol"]
    price = report["price"]
    iv = report["iv"]
    iv_p = report["iv_p"]
    hv = report["hv"]
    status = report["status"]
    strategy = report["strategy"]
    trigger_logic = report["trigger_logic"]
    days_to_earnings = report["days_to_earnings"]
    stop_loss = report["stop_loss"]
    daily_theta = report["daily_theta"]
    runway_impact = report["runway_impact"]

    # 決定顏色 (Buy Signal = Green, Watchlist Alert = Yellow)
    color = 0x00FF00 if status == "波動率極低" else 0xFFFF00

    embed = discord.Embed(
        title="📊 Nexus Seeker | 波動率優勢偵測",
        color=color,
        timestamp=datetime.now(timezone.utc),
    )

    # Field 1: [Symbol] 戰略評估
    v1_headers = ["評估指標", "數據值"]
    v1_widths = [20, 20]
    val1_lines = ["```ansi"]
    val1_lines.append(
        " | ".join(_pad_string(h, w) for h, w in zip(v1_headers, v1_widths))
    )
    val1_lines.append("-" * (sum(v1_widths) + 3))
    val1_lines.append(
        f"{_pad_string('當前價格 (Price)', v1_widths[0])} | {_pad_string(f'${price:.2f}', v1_widths[1])}"
    )
    val1_lines.append(
        f"{_pad_string('IV / IV Percentile', v1_widths[0])} | {_pad_string(f'{iv}% ({iv_p}%)', v1_widths[1])}"
    )
    val1_lines.append(
        f"{_pad_string('HV (252-day)', v1_widths[0])} | {_pad_string(f'{hv}%', v1_widths[1])}"
    )
    status_colored = (
        f"\033[0;32m{status}\033[0m"
        if status == "波動率極低"
        else f"\033[0;33m{status}\033[0m"
    )
    val1_lines.append(
        f"{_pad_string('目前狀態 (Status)', v1_widths[0])} | {_pad_string(status, v1_widths[1]).replace(status, status_colored)}"
    )
    val1_lines.append("```")
    val1 = "\n".join(val1_lines)
    embed.add_field(name=f"🔍 {sym} 戰略評估", value=val1, inline=False)

    # Field 2: 買入時機分析
    catalyst = (
        f"距離財報 {days_to_earnings} 天" if days_to_earnings <= 90 else "無近期財報"
    )
    val2_lines = ["```ansi"]
    val2_lines.append(f"建議策略 (Strategy): {strategy}")
    val2_lines.append(f"觸發邏輯 (Trigger) : {trigger_logic}")
    val2_lines.append(f"催化因子 (Catalyst): {catalyst}")
    val2_lines.append("```")
    val2 = "\n".join(val2_lines)
    embed.add_field(name="🎯 買入時機分析", value=val2, inline=False)

    # Field 3: 風險管理 (NRO)
    v3_headers = ["風控指標", "評估數據"]
    v3_widths = [22, 18]
    val3_lines = ["```ansi"]
    val3_lines.append(
        " | ".join(_pad_string(h, w) for h, w in zip(v3_headers, v3_widths))
    )
    val3_lines.append("-" * (sum(v3_widths) + 3))

    val3_lines.append(
        f"{_pad_string('建議停損 (Stop Loss)', v3_widths[0])} | {_pad_string(f'${stop_loss:.2f}', v3_widths[1])}"
    )

    theta_str = f"-${daily_theta:.2f}/day"
    val3_lines.append(
        f"{_pad_string('Theta 每日損耗', v3_widths[0])} | {_pad_string(theta_str, v3_widths[1]).replace(theta_str, f'\033[0;31m{theta_str}\033[0m')}"
    )

    runway_str = f"{runway_impact} 天"
    val3_lines.append(
        f"{_pad_string('預估跑道影響', v3_widths[0])} | {_pad_string(runway_str, v3_widths[1])}"
    )
    val3_lines.append("```")
    val3 = "\n".join(val3_lines)
    embed.add_field(name="🛡️ 風險管理 (NRO)", value=val3, inline=False)

    embed.set_footer(text="Nexus Volatility Strategist Agent v1.0 | 專注廉價波動率偵測")
    return embed


def build_hedge_analysis_field(embed, analysis):
    """
    在 embed 中加入對沖分析區塊。
    """
    if not isinstance(analysis, dict):
        embed.add_field(
            name="🛡️ 對沖有效性診斷",
            value=_safe_embed_field_value(
                "目前無法取得對沖分析資料。", "目前無法取得對沖分析資料。"
            ),
            inline=False,
        )
        return

    status = str(analysis.get("status", "UNKNOWN"))
    status_emoji = "✅" if status == "OPTIMAL" else "⚠️"
    effectiveness = _safe_float(analysis.get("effectiveness", 0.0), 0.0)
    alpha_contribution = _safe_float(analysis.get("alpha_contribution", 0.0), 0.0)
    hedge_contribution = _safe_float(analysis.get("hedge_contribution", 0.0), 0.0)
    hedge_ratio = _safe_float(analysis.get("hedge_ratio", 0.0), 0.0)
    net_pnl = _safe_float(analysis.get("net_pnl", 0.0), 0.0)

    # 決定有效性評價
    if effectiveness >= 0.8:
        eff_text = "🎯 精準"
    elif effectiveness >= 0.6:
        eff_text = "⚖️ 適中"
    else:
        eff_text = "🌪️ 偏差"

    content = (
        f"🔹 **個股 Alpha 損益**: `${alpha_contribution:,.2f}`\n"
        f"🔸 **對沖 Beta 損益**: `${hedge_contribution:,.2f}`\n"
        f"📊 **對沖比率 (HR)**: `{hedge_ratio:.2%}` {status_emoji}\n"
        f"🧩 **對沖有效性 (ES)**: `{effectiveness:.2%}` ({eff_text})\n"
        f"🏁 **最終淨損益**: **`${net_pnl:,.2f}`**"
    )

    embed.add_field(
        name="🛡️ 對沖有效性診斷",
        value=_safe_embed_field_value(content, "對沖分析資料不足。"),
        inline=False,
    )


# ============================================================================
# Common notification, calendar, and service-operation embeds
# ============================================================================


def create_ai_analysis_embed(
    ai_report_text: str, title: str = "📊 Nexus Seeker 盤後 AI 深度分析"
) -> discord.Embed:
    """
    將 AI 產出的盤後深度分析轉換為 Discord Embed 格式。
    參考盤後風險結算報告風格。
    """
    embed = discord.Embed(
        title=title,
        color=_report_embed_color(ai_report_text),
        timestamp=datetime.now(timezone.utc),
    )

    sections = _parse_ai_report_sections(ai_report_text)
    if sections:
        for header, content in sections:
            embed.add_field(
                name=header,
                value=_safe_embed_field_value(content, "無詳細資訊"),
                inline=False,
            )
    else:
        embed.description = _truncate_with_boundary(ai_report_text, 4000)

    embed.set_footer(text="Nexus Seeker AI Analyst v1.0 | 智慧投研管線")
    return embed


def create_next_day_strategy_embed(strategy_text: str) -> discord.Embed:
    """
    將次日策略制定報告轉換為 Discord Embed 格式。
    參考盤後風險結算報告風格。
    """
    embed_color = discord.Color.blue()
    if "🚨" in strategy_text or "🆘" in strategy_text:
        embed_color = discord.Color.red()
    elif "⚠️" in strategy_text:
        embed_color = discord.Color.orange()

    embed = discord.Embed(
        title="🎯 Nexus Seeker 次日策略制定",
        color=embed_color,
        timestamp=datetime.now(timezone.utc),
    )

    # 策略報告通常包含 VIX 資訊與戰術建議
    # 嘗試切分 **標題**:
    sections = re.split(r"\n(?=\*\*[^*]+\*\*[:：])", strategy_text)

    if len(sections) > 1:
        # 第一部分可能是時間標題行，放在 description
        first_part = sections[0].strip()
        # 移除時間標題與分隔線
        first_part = re.sub(r"\*\*.*次日策略制定\*\*", "", first_part)
        first_part = re.sub(r"-{10,}", "", first_part).strip()

        if first_part:
            embed.description = first_part

        # 處理後續欄位
        for section in sections[1:]:
            section = section.strip()
            if not section:
                continue

            lines = section.split("\n", 1)
            header = (
                lines[0].replace("**", "").replace(":", "").replace("：", "").strip()
            )
            content = lines[1].strip() if len(lines) > 1 else "無內容"

            panel = _build_watchlist_style_panel(
                f"{header} (Next-Day Strategy)",
                content,
                width=45,
                empty_msg="無詳細資訊",
            )
            embed.add_field(
                name=header,
                value=_safe_embed_codeblock_value(panel, "無詳細資訊", lang="ansi"),
                inline=False,
            )
    else:
        # 移除標題行後放入欄位 (全部內容也用文字面板呈現)
        clean_text = re.sub(r"\*\*.*次日策略制定\*\*", "", strategy_text)
        clean_text = re.sub(r"-{10,}", "", clean_text).strip()
        embed.description = "以下為次日策略摘要，建議依序閱讀各區塊。"
        panel = _build_watchlist_style_panel(
            "🎯 次日策略摘要 (Next-Day Summary)",
            clean_text,
            width=45,
            empty_msg="無詳細資訊",
        )
        embed.add_field(
            name="🎯 次日策略摘要",
            value=_safe_embed_codeblock_value(panel, "無詳細資訊", lang="ansi"),
            inline=False,
        )

    embed.set_footer(text="Nexus Seeker Strategy Engine v1.0 | 戰鬥階級系統")
    return embed


def create_notification_settings_embed(
    scheduled_list: list, realtime_list: list, polymarket_list: list
) -> discord.Embed:
    """建立自訂通知設定偏好中心 Embed"""
    embed = discord.Embed(
        title="🌌 Nexus Seeker ｜ 通知偏好設定中心",
        description="請使用下方下拉選單點擊要切換的項目，或使用一鍵按鈕管理所有通知。\n🟢 代表開啟，🔴 代表關閉。",
        color=discord.Color.dark_magenta(),
        timestamp=datetime.now(timezone.utc),
    )
    embed.add_field(
        name="📅 定時與掃描背景通知 (Scheduled & Scan)",
        value="\n".join(scheduled_list),
        inline=False,
    )
    embed.add_field(
        name="⚡ 即時風險與事件警報 (Real-time & Events)",
        value="\n".join(realtime_list),
        inline=False,
    )
    embed.add_field(
        name="🐳 Polymarket 巨鯨與 AI 監控 (Polymarket Settings)",
        value="\n".join(polymarket_list),
        inline=False,
    )
    embed.set_footer(text="Quantitative Preferences | Ephemeral Configuration")
    return embed


def create_account_settings_embed(
    basic_settings: list, runway_settings: list
) -> discord.Embed:
    """建立帳戶全域參數配置中心 Embed"""
    embed = discord.Embed(
        title="🌌 Nexus Seeker ｜ 帳戶全域參數配置中心",
        description="請使用下方下拉選單選擇想要更改的參數。\n布林值項目將會立即切換，數值項目將會彈出輸入框供您修改。",
        color=discord.Color.blue(),
        timestamp=datetime.now(timezone.utc),
    )
    embed.add_field(
        name="📊 核心帳戶與交易參數 (Core Settings)",
        value="\n".join(basic_settings),
        inline=False,
    )
    embed.add_field(
        name="💸 財務生存跑道指標 (Runway Settings)",
        value="\n".join(runway_settings),
        inline=False,
    )
    embed.set_footer(text="Quantitative Preferences | Ephemeral Configuration")
    return embed


def create_info_embed(title: str, message: str) -> discord.Embed:
    """建立標準資訊通知 Embed"""
    embed = discord.Embed(
        title=f"ℹ️ {title}",
        description=message,
        color=discord.Color.blue(),
        timestamp=datetime.now(timezone.utc),
    )
    embed.set_footer(text="Nexus Seeker | System Notification")
    return embed


def create_error_embed(message: str, title: str = "系統錯誤") -> discord.Embed:
    """建立標準錯誤通知 Embed"""
    embed = discord.Embed(
        title=f"❌ {title}",
        description=message,
        color=discord.Color.red(),
        timestamp=datetime.now(timezone.utc),
    )
    embed.set_footer(text="Nexus Seeker | Error Report")
    return embed


def create_max_pain_embed(symbol: str, data: Dict[str, Any]) -> discord.Embed:
    """建立最大痛點分析 Embed。"""
    embed = discord.Embed(
        title=f"📍 {symbol} 最大痛點分析 (Max Pain)",
        color=discord.Color.blue(),
        timestamp=datetime.now(timezone.utc),
    )
    embed.add_field(name="到期日", value=f"`{data.get('expiry', 'N/A')}`", inline=True)
    embed.add_field(
        name="最大痛點 Strike",
        value=f"**${data.get('max_pain', 'N/A')}**",
        inline=True,
    )
    embed.add_field(
        name="目前價格",
        value=f"`${data.get('current_price', 'N/A')}`",
        inline=True,
    )

    distance_pct = _safe_float(data.get("distance_pct"))
    distance_text = (
        f"現價高於痛點 `{distance_pct:.2f}%`"
        if distance_pct > 0
        else f"現價低於痛點 `{abs(distance_pct):.2f}%`"
        if distance_pct < 0
        else "現價貼近最大痛點 `0.00%`"
    )
    embed.add_field(name="偏離度", value=distance_text, inline=False)

    if data.get("is_converging"):
        embed.description = "🎯 **價格正向最大痛點收斂中** (預期結算日波動縮小)"

    expiry = data.get("expiry")
    if isinstance(expiry, str):
        try:
            expiry_dt = datetime.strptime(expiry, "%Y-%m-%d")
            dte = (expiry_dt - datetime.now()).days
            if dte <= 3:
                embed.add_field(
                    name="🚀 執行建議",
                    value="⚠️ **DTE < 3 且接近最大痛點**\n建議提升 **獲利鎖定 (Profit Lock)** 優先級，規避結算震盪。",
                    inline=False,
                )
        except ValueError:
            pass

    embed.set_footer(text="Nexus Seeker | Execution Automation")
    return embed


def create_financial_runway_embed(
    cash_reserve: float,
    monthly_expense: float,
    total_theta: float,
    runway_days: float,
    backup_liquidity: float = 0.0,
    extended_runway: float | None = None,
    total_holding_value: float = 0.0,
    ratio: float | None = None,
    footer_text: str = "Nexus Seeker | Financial Runway",
) -> discord.Embed:
    """建立財務生存跑道 Embed。"""
    color = discord.Color.green() if runway_days > 180 else discord.Color.orange()
    embed = discord.Embed(
        title="🏁 財務生存跑道分析",
        color=color,
        timestamp=datetime.now(timezone.utc),
    )
    embed.add_field(name="💰 現金儲備", value=f"`${cash_reserve:,.2f}`", inline=True)
    embed.add_field(name="📉 每月支出", value=f"`${monthly_expense:,.2f}`", inline=True)
    embed.add_field(
        name="💸 每日 Theta",
        value=f"`+${total_theta:,.2f}/day`",
        inline=True,
    )
    runway_text = (
        f"**{runway_days:,.1f} 天**"
        if runway_days < 9999
        else "**♾️ 無限 (收益已覆蓋支出)**"
    )
    embed.add_field(
        name="⌛ 核心生存跑道",
        value=runway_text,
        inline=False,
    )
    if backup_liquidity > 0 and extended_runway is not None:
        extended_text = (
            f"**{extended_runway:,.1f} 天**" if extended_runway < 9999 else "**♾️ 無限**"
        )
        embed.add_field(
            name="🛡️ 備用流動性",
            value=(
                f"`${total_holding_value:,.2f}` (折價後: `${backup_liquidity:,.2f}`)\n"
                f"預計可將跑道延長至: {extended_text}"
            ),
            inline=False,
        )
    if ratio is not None:
        embed.add_field(
            name="📊 收益支出比 (Theta/Expense)", value=f"`{ratio:.2%}`", inline=True
        )
    embed.set_footer(text=footer_text)
    return embed


def create_system_health_embed(
    *,
    memory_percent: float,
    memory_available_mb: float,
    cpu_percent: float,
    process_memory_mb: float,
    disk_percent: float,
    disk_free_gb: float,
    sma_cache_size: int,
    ema_cache_size: int,
    poly_cache_size: int = 0,
    orderbook_size: int = 0,
) -> discord.Embed:
    """建立系統健康診斷 Embed。"""
    is_healthy = memory_percent < 80 and disk_percent < 85
    embed = discord.Embed(
        title="🖥️ Nexus Seeker 系統健康診斷",
        color=discord.Color.green() if is_healthy else discord.Color.red(),
        timestamp=datetime.now(timezone.utc),
    )
    embed.add_field(
        name="VPS 記憶體",
        value=f"`{memory_percent}%` (可用: {memory_available_mb:.1f}MB)",
        inline=True,
    )
    embed.add_field(name="CPU 負載", value=f"`{cpu_percent}%`", inline=True)
    embed.add_field(
        name="程序占用 (RSS)", value=f"`{process_memory_mb:.1f} MB`", inline=True
    )
    embed.add_field(
        name="💿 硬碟空間",
        value=f"`{disk_percent}%` (可用: {disk_free_gb:.1f}GB)",
        inline=True,
    )
    cache_info = (
        f"• SMA/EMA Cache: `{sma_cache_size}/{ema_cache_size}`\n"
        f"• Poly Markets: `{poly_cache_size}`\n"
        f"• OrderBooks: `{orderbook_size}`"
    )
    embed.add_field(name="📦 快取統計 (LRU/Bounded)", value=cache_info, inline=False)

    health_status = "✅ 狀態優良"
    if memory_percent > 85 or disk_percent > 85:
        health_status = "⚠️ **資源吃緊**"
        if memory_percent > 85:
            health_status += " (記憶體閾值已達)"
        if disk_percent > 85:
            health_status += " (磁碟空間不足)"

    if memory_percent > 95 or disk_percent > 95:
        health_status = "🆘 **極度危險**"
        if memory_percent > 95:
            health_status += " (OOM 警告)"
        if disk_percent > 95:
            health_status += " (磁碟即將滿載)"

    embed.add_field(name="🩺 健康評級", value=health_status, inline=False)
    embed.set_footer(text="Argo Optimization Engine | Low-RAM VPS Edition")
    return embed


def create_asset_promotion_embed(
    symbol: str,
    expiry: str,
    strike: float,
    opt_type: str,
    quantity: int,
    price: float,
) -> discord.Embed:
    """建立 WATCH -> TRADE 晉升成功 Embed。"""
    embed = discord.Embed(
        title="🌌 Nexus | 資產晉升成功",
        description=f"標的 **{symbol}** 已從「觀察」提升為「實單交易」。",
        color=0x00FF7F,
        timestamp=datetime.now(timezone.utc),
    )
    embed.add_field(
        name="合約細節",
        value=f"`{expiry}` ${strike} {opt_type.upper()}\n數量: `{quantity}` 口 | 價格: `${price}`",
    )
    embed.set_footer(text="Unified Asset Lifecycle v1.0")
    return embed


def create_transition_simulation_embed(
    *,
    symbol: str,
    current_price: float,
    initial_pnl: float,
    additional_capital_required: float,
    adjusted_cost_basis: float,
    target_cc_strike: float,
    target_cc_premium: float,
    projected_aroc: float,
    capital_efficiency_gain: float,
) -> discord.Embed:
    """建立戰略轉軌模擬 Embed。"""
    embed = discord.Embed(
        title=f"🔄 戰略轉軌模擬 (演進) | {symbol}",
        description=f"模擬將 `{symbol}` 投機期權部位演進為 **核心現股 + 備兌買權 (Covered Call)** 模型。",
        color=discord.Color.blue(),
        timestamp=datetime.now(timezone.utc),
    )
    embed.add_field(name="現價 (Price)", value=f"`${current_price:.2f}`", inline=True)
    embed.add_field(
        name="期權獲利 (Option PnL)", value=f"`${initial_pnl:,.2f}`", inline=True
    )
    roadmap = (
        "1. **執行動作**：平倉現有 DITM 部位，回收收益。\n"
        f"2. **購入現股**：以 `${current_price:.2f}` 購入 100 股。\n"
        f"3. **追加資本**：需額外投入 **`${additional_capital_required:,.2f}`**。\n"
        f"4. **成本調整**：調整後每股成本為 **`${adjusted_cost_basis:.2f}`**。\n"
        f"5. **建立 CC**：賣出 `${target_cc_strike}` Call，收取 `${target_cc_premium:.2f}` 權利金。"
    )
    embed.add_field(name="🚀 資本重分配路線圖 (Roadmap)", value=roadmap, inline=False)
    efficiency = (
        f"• **預期年化回報 (AROC)**：`{projected_aroc:.1f}%` "
        f"{'✅ 符合 15% 門檻' if projected_aroc >= 15 else '⚠️ 低於效率門檻'}\n"
        f"• **單次收租殖利率**：`{capital_efficiency_gain:.2f}%`"
    )
    embed.add_field(name="📊 資本效率評估", value=efficiency, inline=False)
    embed.set_footer(text="戰略轉軌引擎 v1.0 | 專業營運模式")
    return embed


def create_market_calendar_embed(
    events: List[Any],
    *,
    max_items: int = 15,
    empty_message: str = "📭 未來 7 日內無重大事件。",
) -> discord.Embed:
    """建立市場事件與財報日曆 Embed。"""
    if not events:
        return create_info_embed("查無資料", empty_message)

    embed = discord.Embed(
        title="📅 【 重大市場事件 & 財報日曆 】",
        color=discord.Color.blue(),
        timestamp=datetime.now(timezone.utc),
    )

    for event in events[:max_items]:
        event_name = getattr(event, "event", None)
        symbol = getattr(event, "symbol", None)
        tte_hours = getattr(event, "tte_hours", "N/A")
        event_date = getattr(event, "date", None)
        country = getattr(event, "country", None)
        impact = str(getattr(event, "impact", "")).lower()
        event_time = getattr(event, "time", None)

        if event_name is not None:
            impact_icon = "🔴" if impact == "high" else "🟡"
            field_name = (
                f"{impact_icon} {event_name} ({country})"
                if country
                else f"{impact_icon} {event_name}"
            )
            time_part = f" | `{event_time}`" if event_time else ""
            field_value = f"⏰ TTE: `{tte_hours}`h{time_part}"
        elif symbol is not None:
            field_name = f"📊 {symbol} 財報發布"
            date_part = f" | `{event_date}`" if event_date else ""
            field_value = f"⏰ TTE: `{tte_hours}`h{date_part}"
        else:
            continue

        embed.add_field(name=field_name, value=field_value, inline=False)

    embed.set_footer(text="Calendar-Aware Guard | Nexus Seeker")
    return embed


def create_iv_risk_scan_embed(results: List[Dict[str, Any]]) -> discord.Embed:
    """建立高 IV / IV Crush 風險掃描 Embed。"""
    if not results:
        return create_info_embed("系統資訊", "🔎 未發現 IV Rank > 80% 的高波動標的。")

    embed = discord.Embed(
        title="🔥 【 高波動 & IV Crush 風險掃描 】",
        color=discord.Color.red(),
        timestamp=datetime.now(timezone.utc),
    )
    for res in results[:15]:
        risk_icon = "🚨" if res.get("is_high_risk_vol") else "⚠️"
        embed.add_field(
            name=f"{risk_icon} {res.get('symbol', 'N/A')} (IVR: {res.get('iv_rank', 0)}%)",
            value=(
                f"TTE: `{_safe_float(res.get('tte_hours')):.1f}`h | "
                f"策略: {res.get('strategy', 'N/A')}"
            ),
            inline=False,
        )
    embed.set_footer(text="IV Rank Scanner | Nexus Seeker")
    return embed


def create_event_impact_embed(
    symbol: str,
    vol_move: float,
    total_delta: float,
    total_vanna: float,
    adjusted_delta: float,
    delta_shift: float,
    exposure_shift_dollars: float,
) -> discord.Embed:
    """建立事件風險 What-if 模擬 Embed。"""
    embed = discord.Embed(
        title=f"🎲 【 {symbol} 事件風險模擬 (What-if) 】",
        description=f"假設波動率變動 `{vol_move}%` 時，部位 Greeks 的動態偏移：",
        color=discord.Color.gold(),
        timestamp=datetime.now(timezone.utc),
    )

    embed.add_field(
        name="目前 Beta-Weighted Delta",
        value=f"`{total_delta:.2f}`",
        inline=True,
    )
    embed.add_field(
        name="目前 Vanna (曝險變化率)",
        value=f"`{total_vanna:.2f}`",
        inline=True,
    )
    embed.add_field(
        name="預期 Hidden Delta",
        value=f"`{adjusted_delta:.2f}`",
        inline=False,
    )
    embed.add_field(name="Delta 偏移量", value=f"`{delta_shift:+.2f}`", inline=True)
    embed.add_field(
        name="等值曝險變動 (USD)",
        value=f"`${exposure_shift_dollars:,.2f}`",
        inline=True,
    )

    risk_status = "🔴 危險" if abs(adjusted_delta) > 100 else "🟢 安全"
    embed.add_field(name="風險狀態判定", value=f"**{risk_status}**", inline=False)
    embed.set_footer(text="NRO Vanna Simulation | Nexus Seeker")
    return embed


def create_hedge_settlement_embed(
    alert_id: int, hedge_instrument: str, executed_quantity: int
) -> discord.Embed:
    """建立對沖結算完成 Embed。"""
    embed = discord.Embed(
        title="✅ 對沖結算完成",
        description=f"已成功記錄警報 `#{alert_id}` 的對沖執行紀錄。",
        color=discord.Color.green(),
        timestamp=datetime.now(timezone.utc),
    )
    embed.add_field(name="執行標的", value=f"`{hedge_instrument}`", inline=True)
    embed.add_field(name="執行數量", value=f"`{executed_quantity}`", inline=True)
    embed.set_footer(text="數據已同步至 SQLite 持久化層，可用於歸因分析。")
    return embed


def create_hedge_list_embed(rows: List[Any]) -> discord.Embed:
    """建立最近對沖警報列表 Embed。"""
    embed = discord.Embed(
        title="📜 最近對沖警報列表",
        color=discord.Color.blue(),
        timestamp=datetime.now(timezone.utc),
    )

    content: List[str] = []
    for row in rows:
        status_emoji = (
            "⏳" if row[3] == "PENDING" else "✅" if row[3] == "EXECUTED" else "❌"
        )
        content.append(
            f"`#{row[0]}` | {status_emoji} | VIX: `{row[1]:.2f}` | 建議: `{row[2]}`股 | {row[4][:16]}"
        )

    embed.description = "\n".join(content) if content else "📭 目前無對沖警報紀錄。"
    embed.set_footer(text="Nexus Seeker | Hedge Ledger")
    return embed


def create_hedge_alert_embed(
    *,
    vix: float,
    stage_move: int,
    tier_name: str,
    tier_emoji: str,
    color_hex: int,
    total_beta_delta: float,
    adjusted_delta: float,
    total_vega: float,
    hedge_quantity: int,
    instruction_text: str,
    narration: str,
    alert_id: int,
    poly_snapshot: Optional[List[Dict[str, Any]]] = None,
) -> discord.Embed:
    """建立自動化對沖警報 Embed。"""
    embed = discord.Embed(
        title="🚨 【戰位報告：自動化對沖警報】",
        description=f"**警報等級：** {tier_emoji} {tier_name} (移動 `{stage_move:+} 階`)",
        color=discord.Color(color_hex),
        timestamp=datetime.now(timezone.utc),
    )

    embed.add_field(
        name="📊 風險指標",
        value=(
            f"• **即時 VIX:** `{vix:.2f}`\n"
            f"• **淨 Delta:** `{total_beta_delta:+.1f}`\n"
            f"• **調整後 Delta:** `{adjusted_delta:+.1f}` (Hidden Delta)\n"
            f"• **Vega 脆弱性:** `{total_vega:+.2f}`"
        ),
        inline=False,
    )

    if poly_snapshot:
        snapshot_lines: List[str] = []
        for event in poly_snapshot:
            question = str(event.get("question", "N/A"))
            truncated = question[:40] + "..." if len(question) > 40 else question
            odds = event.get("odds_distribution", [])
            odds_str = " | ".join(
                f"{item.get('outcome')}: `{item.get('odds', 0.0) * 100:.0f}%`"
                for item in odds[:2]
            )
            snapshot_lines.append(f"• **{truncated}**\n  └ {odds_str}")

        if snapshot_lines:
            embed.add_field(
                name="🌐 [快取快照] Polymarket 即時機率",
                value="\n".join(snapshot_lines),
                inline=False,
            )

    embed.add_field(name="🤖 AI 風險敘述", value=f"*{narration}*", inline=False)
    embed.add_field(
        name="🛡️ 對沖建議指令",
        value=f"```fix\n{instruction_text}\n```",
        inline=False,
    )
    embed.add_field(
        name="📈 預期效果",
        value=(
            f"執行後淨 Delta 將回歸至 `{adjusted_delta + (hedge_quantity * -1.0):+.1f}` 附近，"
            "顯著降低系統性回撤風險。"
        ),
        inline=False,
    )
    embed.set_footer(text=f"Nexus Seeker Battle Station | Alert ID: {alert_id}")
    return embed


def create_proactive_event_alert_embed(events: List[Any]) -> List[discord.Embed]:
    """建立重大事件主動預警 Embed。"""
    embed = discord.Embed(
        title="🛡️ 【 預警：重大事件即時防護 】",
        description=(
            "偵測到您的持倉標的即將迎來重大波動事件；以下 NRO 指令已依事件類型、"
            "剩餘時間與目前持倉風險狀態調整。"
        ),
        color=discord.Color.dark_red(),
        timestamp=datetime.now(timezone.utc),
    )

    if not events:
        embed.description = "📭 目前沒有偵測到即將來臨的重大波動事件。"
        embed.set_footer(text="Proactive Event Monitor | Nexus Seeker")
        return [embed]

    for event in events:
        if isinstance(event, dict):
            name = str(event.get("name", "⚠️ 重大事件"))
            tte_hours = float(event.get("tte_hours", 0.0) or 0.0)
            risk_status = str(event.get("risk_status", "持倉風險狀態暫不可用"))
            instruction = str(event.get("instruction", "請先降低曝險並觀察事件落地。"))
        else:
            if event.type == "ECONOMIC":
                name = f"🔴 經濟數據: {event.event}"
                instruction = "增加 Vanna 權重，縮減賣方曝險。"
            else:
                name = f"📊 財報預警: {event.symbol}"
                instruction = "已啟動 IV Crush 防護機制。"
            tte_hours = float(getattr(event, "tte_hours", 0.0) or 0.0)
            risk_status = "持倉風險狀態未提供，請搭配組合 Greeks 自行覆核。"

        # Determine colors for TTE
        if tte_hours <= 12.0:
            tte_color = "\u001b[1;31m"  # Urgent Red
        elif tte_hours <= 24.0:
            tte_color = "\u001b[1;33m"  # Warning Yellow
        else:
            tte_color = "\u001b[1;36m"  # Safe Cyan

        # Determine colors for Risk Status
        risk_str = str(risk_status)
        if any(
            w in risk_str
            for w in ["危險", "警告", "高", "🚨", "⚠️", "賣方偏重", "短 Gamma"]
        ):
            risk_color = "\u001b[1;31m"  # High Risk Red
        elif any(w in risk_str for w in ["安全", "低", "良好", "正常"]):
            risk_color = "\u001b[1;32m"  # Low Risk Green
        else:
            risk_color = "\u001b[1;33m"  # Neutral Yellow

        event_lines = [
            "```ansi",
            " 🛡️ 即時防護狀態 (Event Risk Protection)",
            " ----------------------------------",
            " 距離發布 (Time to Event)",
            f" └─ 剩餘時間: {tte_color}{tte_hours:.1f} 小時\u001b[0m",
            " 持倉風險狀態 (Position Risk Status)",
            f" └─ 狀態: {risk_color}{risk_status}\u001b[0m",
            " NRO 指令 (NRO Instruction)",
            f" └─ 指令: \u001b[1;36m{instruction}\u001b[0m",
            "```",
        ]
        value = "\n".join(event_lines)

        embed.add_field(name=name, value=value, inline=False)

    embed.set_footer(text="Proactive Event Monitor | Nexus Seeker")
    return split_embed_by_fields(embed)


def create_memory_alert_embed(
    total_usage: float,
    process_memory_mb: float,
    sma_cache_size: int,
    ema_cache_size: int,
) -> discord.Embed:
    """建立記憶體不足緊急警報 Embed。"""
    embed = discord.Embed(
        title="🆘 【系統緊急警報：記憶體不足】",
        description=(
            f"VPS 記憶體使用量已達臨界值 (`{total_usage}%`)，"
            "可能導致程序被 OOM Killer 終止。"
        ),
        color=discord.Color.red(),
        timestamp=datetime.now(timezone.utc),
    )
    embed.add_field(name="當前總占用", value=f"`{total_usage}%`", inline=True)
    embed.add_field(
        name="程序占用 (RSS)", value=f"`{process_memory_mb:.1f} MB`", inline=True
    )
    embed.add_field(
        name="📦 快取消費者",
        value=f"SMA/EMA: `{sma_cache_size}/{ema_cache_size}` 筆",
        inline=False,
    )
    embed.set_footer(text="建議重啟服務或增加 Swap 分區。")
    return embed


def create_polymarket_whale_alert_embed(
    *,
    intent_emoji: str,
    intent_label: str,
    market_question: str,
    usd_value: float,
    dynamic_threshold: float,
    win_rate: float,
    is_high_conviction: bool,
    is_bullish: bool,
    summary: str,
    event_slug: Optional[str] = None,
    uoa_correlation: Optional[Dict[str, Any]] = None,
) -> discord.Embed:
    """建立 Polymarket 巨鯨戰報 Embed。"""
    embed = discord.Embed(
        title="【 🐋 Polymarket 巨鯨戰報 】"
        + (" 🔥 高信心訊號" if is_high_conviction else ""),
        color=(
            discord.Color.gold()
            if is_high_conviction
            else (discord.Color.blue() if is_bullish else discord.Color.red())
        ),
        timestamp=datetime.now(timezone.utc),
    )

    content = [
        f"## {intent_emoji} {intent_label}",
        "---",
        f"**市場問題：** **{market_question}**",
        f"**交易金額：** `${usd_value:,.2f}`",
        f"**流動性倍數：** `{usd_value / dynamic_threshold:.2f}x`",
        f"**當前勝率：** {win_rate:.1f}%",
        "---",
    ]

    if uoa_correlation:
        uoa = uoa_correlation["uoa"]
        classification = uoa_correlation["classification"]
        content.append(f"🔍 **UOA 關聯偵測 ({uoa['symbol']})**")
        content.append(f"- 合約: `{uoa['expiry']}` `${uoa['strike']}` {uoa['type']}")
        content.append(
            f"- 性質: **{classification['classification']}** (信心: `{classification['confidence']:.2f}`)"
        )
        content.append(f"- 理由: {classification['explanation']}")
        content.append("---")

    if win_rate > 70 or win_rate < 30:
        content.append("🛡️ **【預測性對沖建議 (Predictive Hedge)】**")
        content.append(
            f"偵測到預測市場對 `{market_question}` 的機率激增至 `{win_rate:.1f}%`，建議提前在 VTR 執行 Delta 對沖，以應對潛在的波動率跳空。"
        )
        content.append("---")

    if summary and summary != "（未啟用 AI 分析）":
        content.append(f"**🤖 AI 總結分析**\n{summary}")
        content.append("---")

    market_url = (
        f"https://polymarket.com/event/{event_slug}"
        if event_slug
        else "https://polymarket.com"
    )
    content.append(f"[🔗 前往市場]({market_url})")

    embed.description = "\n".join(content)
    embed.set_footer(
        text=f"Nexus Seeker 監測系統 | 動態門檻: ${dynamic_threshold:,.0f}"
    )
    return embed


# ============================================================================
# Intraday pipeline and automation-specific embeds
# ============================================================================


def create_intraday_scan_embed(output) -> discord.Embed:
    """建立盤中量化掃描與避險執行指南的 Discord Embed"""
    route_icon = (
        "🏹 SPEAR"
        if output.sddm_route == "SPEAR"
        else ("🛡️ SHIELD" if output.sddm_route == "SHIELD" else "⏳ WAIT")
    )

    embed_color = discord.Color.green()
    if output.sddm_route == "SHIELD":
        embed_color = (
            discord.Color.red() if output.failed_gates else discord.Color.orange()
        )
    elif output.sddm_route == "WAIT":
        embed_color = discord.Color.blue()

    embed = discord.Embed(
        title=f"📊 Nexus Seeker | 盤中量化掃描 & 避險執行指南 ({output.ticker})",
        color=embed_color,
        timestamp=output.timestamp.astimezone(timezone.utc),
    )

    # Monospace block 1: Account survival runway
    runway_block = [
        "```ansi",
        "\033[1;36m[帳戶生存與財務跑道評估]\033[0m",
        f"存活天數 : {output.financial_runway_days} 天",
        f"Theta 每日覆蓋率 : {output.theta_coverage_pct:.1f}%",
        f"狀態評估 : {output.runway_status_msg}",
        "```",
    ]
    embed.add_field(name="🏁 財務生存跑道", value="\n".join(runway_block), inline=False)

    # Monospace block 2: Squeeze and gates info
    sddm_color = "\033[1;32m" if output.sddm_route == "SPEAR" else "\033[1;31m"
    gates_status = (
        "全部通過 (PASS)"
        if not output.failed_gates
        else f"未通過 ({len(output.failed_gates)} 項門檻)"
    )

    gates_block = [
        "```ansi",
        "\033[1;36m[戰術決策與門檻狀態]\033[0m",
        f"市場時段 : {output.market_phase}",
        f"門檻狀態 : {gates_status}",
        f"執行路徑 : {sddm_color}{route_icon}\033[0m",
        "```",
    ]
    embed.add_field(name="🏹 戰術決策路由", value="\n".join(gates_block), inline=False)

    if output.failed_gates:
        failed_block = ["```ansi", "\033[1;31m[未通過之戰術硬性門檻]\033[0m"]
        for fg in output.failed_gates:
            failed_block.append(f"- {fg}")
        failed_block.append("```")
        embed.add_field(
            name="❌ 未達標門檻詳情", value="\n".join(failed_block), inline=False
        )

    # Monospace block 3: Squeeze indicators and execution actions
    action_block = ["```ansi", "\033[1;36m[定量執行指南]\033[0m"]
    if output.magnet_target:
        action_block.append(f"磁吸目標價 : ${output.magnet_target:.2f}")
    action_block.append(f"凱利倉位配比 : {output.kelly_position_scaling * 100:.1f}%")
    action_block.append("--------------------------------------------------")
    for act in output.recommended_actions:
        action_block.append(f" {act}")
    action_block.append("```")
    embed.add_field(name="🎯 戰術執行步驟", value="\n".join(action_block), inline=False)

    # Monospace block 4: Vanna Hedging Instructions
    hedge_block = [
        "```ansi",
        "\033[1;36m[Vanna-Adjusted Delta 對沖指引]\033[0m",
        f"對沖指令 : {output.vanna_hedging_instruction}",
        "```",
    ]
    embed.add_field(name="🛡️ 系統性對沖避險", value="\n".join(hedge_block), inline=False)

    # Monospace block 5: Risk notes
    notes_block = [
        "```ansi",
        "\033[1;33m[風控合規備註]\033[0m",
        f"{output.risk_mitigation_notes}",
        "```",
    ]
    embed.add_field(name="⚠️ 風險管控備註", value="\n".join(notes_block), inline=False)

    embed.set_footer(text="Nexus Risk Optimizer | Intraday Squeeze Scan v1.0")
    return embed


def _build_active_order_ansi_card(order: Dict[str, Any]) -> str:
    """建立單筆委託單 ANSI 卡片文字（供 /list_orders 單筆卡片與彙總列表共用）。"""

    order_type_zh = {
        "MARKET": "\u001b[1;36m市價單 (MARKET)\u001b[0m",
        "LIMIT": "\u001b[1;32m限價單 (LIMIT)\u001b[0m",
        "STOP": "\u001b[1;31m停損單 (STOP)\u001b[0m",
        "STOP_LIMIT": "\u001b[1;33m停損限價單 (STOP_LIMIT)\u001b[0m",
        "TRAILING_STOP_USD": "\u001b[1;35m追蹤停損單 USD (TRAILING_STOP_USD)\u001b[0m",
        "TRAILING_STOP_PCT": "\u001b[1;35m追蹤停損單 PCT (TRAILING_STOP_PCT)\u001b[0m",
    }

    validity_zh = {
        "DAY": "\u001b[1;37m當日有效 (DAY)\u001b[0m",
        "EXT_DAY": "\u001b[1;34m全時段有效 (EXT_DAY)\u001b[0m",
        "NIGHT": "\u001b[1;34m夜盤交易 (NIGHT)\u001b[0m",
        "GTC_90": "\u001b[1;37m90天有效 (GTC_90)\u001b[0m",
    }

    ansi_lines = ["```ansi"]
    ansi_lines.append(
        f" 📂 委託單 ID: \u001b[1;33m{order['id']}\u001b[0m  |  標的: \u001b[1;36m{order['symbol']}\u001b[0m"
    )
    ansi_lines.append(" ----------------------------------")

    type_str = order_type_zh.get(order["order_type"], order["order_type"])
    ansi_lines.append(f"  └─ 訂單類型: {type_str}")

    side = str(order.get("side") or "BUY").upper()
    side_str = (
        "\u001b[1;32m買入 (BUY)\u001b[0m"
        if side == "BUY"
        else "\u001b[1;31m賣出 (SELL)\u001b[0m"
    )
    ansi_lines.append(f"  └─ 委託方向: {side_str}")
    ansi_lines.append(f"  └─ 委託數量: \u001b[1;37m{order['quantity']}\u001b[0m 股")

    val_str = validity_zh.get(order["validity"], order["validity"])
    ansi_lines.append(f"  └─ 有效期限: {val_str}")

    price_conditions = []
    if order.get("order_type") in ("LIMIT", "STOP_LIMIT"):
        price_conditions.append(
            f"限價: \u001b[1;32m${order.get('limit_price', 0.0):.2f}\u001b[0m"
        )
    if order.get("order_type") in ("STOP", "STOP_LIMIT"):
        price_conditions.append(
            f"停損價: \u001b[1;31m${order.get('stop_price', 0.0):.2f}\u001b[0m"
        )
    if order.get("order_type") == "TRAILING_STOP_USD":
        price_conditions.append(
            f"追蹤值: \u001b[1;35m${order.get('trailing_value', 0.0):.2f}\u001b[0m"
        )
    if order.get("order_type") == "TRAILING_STOP_PCT":
        price_conditions.append(
            f"追蹤值: \u001b[1;35m{order.get('trailing_value', 0.0):.2f}%\u001b[0m"
        )

    if price_conditions:
        conds_str = " | ".join(price_conditions)
        ansi_lines.append(f"  └─ 委託條件: {conds_str}")
    else:
        ansi_lines.append("  └─ 委託條件: 預設市價成交")

    ansi_lines.append("```")
    return "\n".join(ansi_lines)


def create_active_order_card_embed(order: Dict[str, Any]) -> discord.Embed:
    """建構單筆待成交委託單卡片（每筆訂單對應一則訊息，以便掛載獨立按鈕）。"""

    embed = discord.Embed(
        title=f"📦 待成交委託單 (ID: {order['id']})",
        description=f"標的：`{order['symbol']}`\n\u200b",
        color=discord.Color.orange(),
        timestamp=datetime.now(timezone.utc),
    )

    card_content = _build_active_order_ansi_card(order)
    embed.add_field(
        name="🧾 委託單明細",
        value=_safe_embed_field_value(card_content, "暫無詳情"),
        inline=False,
    )
    embed.set_footer(text="Nexus Seeker • 待成交委託單管理系統")
    return embed


def create_active_orders_embed(orders: List[Dict[str, Any]]) -> List[discord.Embed]:
    """建構待成交委託單列表報告 Embed (使用 Premium ANSI Card 設計，讓數值高亮且易於閱讀)"""
    embed = discord.Embed(
        title="📋 Nexus Seeker | 待成交委託單列表",
        description="以下是您目前所有活躍且待成交的委託單。點擊下方按鈕可進行撤銷或微調。\n\u200b",
        color=discord.Color.orange(),
        timestamp=datetime.now(timezone.utc),
    )

    if not orders:
        embed.description = "📭 目前沒有任何活躍的待成交委託單。您可以透過 `/order_panel` 建立新的掛單。"
        embed.set_footer(text="Nexus Seeker • 待成交委託單管理系統")
        return [embed]

    order_type_zh = {
        "MARKET": "\u001b[1;36m市價單 (MARKET)\u001b[0m",
        "LIMIT": "\u001b[1;32m限價單 (LIMIT)\u001b[0m",
        "STOP": "\u001b[1;31m停損單 (STOP)\u001b[0m",
        "STOP_LIMIT": "\u001b[1;33m停損限價單 (STOP_LIMIT)\u001b[0m",
        "TRAILING_STOP_USD": "\u001b[1;35m追蹤停損單 USD (TRAILING_STOP_USD)\u001b[0m",
        "TRAILING_STOP_PCT": "\u001b[1;35m追蹤停損單 PCT (TRAILING_STOP_PCT)\u001b[0m",
    }

    validity_zh = {
        "DAY": "\u001b[1;37m當日有效 (DAY)\u001b[0m",
        "EXT_DAY": "\u001b[1;34m全時段有效 (EXT_DAY)\u001b[0m",
        "NIGHT": "\u001b[1;34m夜盤交易 (NIGHT)\u001b[0m",
        "GTC_90": "\u001b[1;37m90天有效 (GTC_90)\u001b[0m",
    }

    for idx, o in enumerate(orders):
        ansi_lines = ["```ansi"]
        ansi_lines.append(
            f" 📂 委託單 ID: \u001b[1;33m{o['id']}\u001b[0m  |  標的: \u001b[1;36m{o['symbol']}\u001b[0m"
        )
        ansi_lines.append(" ----------------------------------")

        type_str = order_type_zh.get(o["order_type"], o["order_type"])
        ansi_lines.append(f"  └─ 訂單類型: {type_str}")

        side = str(o.get("side") or "BUY").upper()
        side_str = (
            "\u001b[1;32m買入 (BUY)\u001b[0m"
            if side == "BUY"
            else "\u001b[1;31m賣出 (SELL)\u001b[0m"
        )
        ansi_lines.append(f"  └─ 委託方向: {side_str}")
        ansi_lines.append(f"  └─ 委託數量: \u001b[1;37m{o['quantity']}\u001b[0m 股")

        val_str = validity_zh.get(o["validity"], o["validity"])
        ansi_lines.append(f"  └─ 有效期限: {val_str}")

        # 價格明細條件解析
        price_conditions = []
        if o.get("order_type") in ("LIMIT", "STOP_LIMIT"):
            price_conditions.append(
                f"限價: \u001b[1;32m${o.get('limit_price', 0.0):.2f}\u001b[0m"
            )
        if o.get("order_type") in ("STOP", "STOP_LIMIT"):
            price_conditions.append(
                f"停損價: \u001b[1;31m${o.get('stop_price', 0.0):.2f}\u001b[0m"
            )
        if o.get("order_type") == "TRAILING_STOP_USD":
            price_conditions.append(
                f"追蹤值: \u001b[1;35m${o.get('trailing_value', 0.0):.2f}\u001b[0m"
            )
        if o.get("order_type") == "TRAILING_STOP_PCT":
            price_conditions.append(
                f"追蹤值: \u001b[1;35m{o.get('trailing_value', 0.0):.2f}%\u001b[0m"
            )

        if price_conditions:
            conds_str = " | ".join(price_conditions)
            ansi_lines.append(f"  └─ 委託條件: {conds_str}")
        else:
            ansi_lines.append("  └─ 委託條件: 預設市價成交")

        ansi_lines.append("```")

        card_content = "\n".join(ansi_lines)
        embed.add_field(
            name=f"📦 委託單 #{idx+1} (ID: {o['id']})",
            value=_safe_embed_field_value(card_content, "暫無詳情"),
            inline=False,
        )

    embed.set_footer(text="Nexus Seeker • 待成交委託單管理系統")
    return split_embed_by_fields(embed)


def create_telemetry_alignment_embed(
    alignment_items: List[Dict[str, Any]], truncated: bool = False
) -> discord.Embed:
    """建構待成交委託單盤中遙測對齊警報 Embed (排版參照 Watchlist 半小時戰報 樹狀格式)"""
    embed = discord.Embed(
        title="📡 待成交委託單 - 盤中每半小時 Telemetry 對齊警報",
        description=(
            "⚠️ **【動態掛單偏離度與尾部風險警報】**\n"
            "偵測到美股市場短線隱含波動率 (IV) 劇烈放大，且期權偏斜（Skew）指標進入極端異常區間：\n\u200b"
        ),
        color=discord.Color.red()
        if any(i.get("is_size_down") for i in alignment_items)
        else discord.Color.orange(),
        timestamp=datetime.now(timezone.utc),
    )

    for o in alignment_items:
        sym = o["symbol"]
        order_id = o["order_id"]
        curr_p = o["current_price"]
        orig_q = o["original_qty"]
        sugg_p = o["suggested_price"]
        sugg_q = o["suggested_qty"]
        is_size_down = o["is_size_down"]

        ansi_lines = ["```ansi"]
        ansi_lines.append(
            f" \u001b[1;36m📐 {sym} 遙測價格對齊 (Telemetry Pricing Alignment)\u001b[0m"
        )
        ansi_lines.append(" ----------------------------------")
        ansi_lines.append(
            f"  ├─ 當前掛單價格: \u001b[1;37m${curr_p:.2f}\u001b[0m (數量: \u001b[1;37m{orig_q:.1f}\u001b[0m 股)"
        )

        sugg_price_color = "\u001b[1;32m" if sugg_p == curr_p else "\u001b[1;33m"
        ansi_lines.append(
            f"  ├─ 遙測最佳防線: {sugg_price_color}${sugg_p:.2f}\u001b[0m (數量: \u001b[1;37m{sugg_q:.1f}\u001b[0m 股)"
        )

        if is_size_down:
            ansi_lines.append(
                "  ├─ 偏離防禦狀態: \u001b[1;31m⚠️ 偏離度與尾部風險過高，面臨被擊穿風險\u001b[0m"
            )
            ansi_lines.append(
                "  └─ \u001b[1;31m⚠️ [尾端風險防禦] 偵測到期權偏斜極端尾端風險。系統已自動攔截並將掛單價格微調至更接近現價，且將掛單數量打 75 折以防禦尾部風險。\u001b[0m"
            )
        else:
            ansi_lines.append(
                "  └─ 偏離防禦狀態: \u001b[1;33m⚠️ 偏離度與尾部風險過高，面臨被擊穿風險\u001b[0m"
            )

        ansi_lines.append("```")
        card_content = "\n".join(ansi_lines)

        embed.add_field(
            name=f"📊 標的代號：{sym} (委託單 ID: {order_id})",
            value=_safe_embed_field_value(card_content, "暫無遙測對齊詳情"),
            inline=False,
        )

    footer_text = ""
    if truncated:
        footer_text += "⚠️ *(由於您的活躍委託單數量較多，部分標的之偏離度警報已被安全省略，請至 `/list_orders` 查看完整列表)*\n\n"
    footer_text += "💡 **建議操作**：請點擊下方綠色按鈕「一鍵套用遙測建議價」，系統將自動調整您的委託價格並下調掛單股數以防守大後方。"

    embed.add_field(
        name="💡 建議操作與指引",
        value=_safe_embed_field_value(footer_text, "請使用下方按鈕進行套用"),
        inline=False,
    )
    embed.set_footer(text="Nexus Seeker • 盤中每半小時 telemetry 對齊警報")
    return embed
