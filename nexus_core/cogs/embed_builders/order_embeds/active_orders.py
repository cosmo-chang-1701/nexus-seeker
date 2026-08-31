"""待成交委託單卡片與列表 Embed 建構函式。"""

from datetime import datetime, timezone
from typing import Any, Dict, List

import discord

from cogs.embed_builders._core import NexusEmbed
from cogs.embed_builders._embed_helpers import (
    _safe_embed_field_value,
    split_embed_by_fields,
)


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

    embed = NexusEmbed(
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
    """建構待成交委託單列表報告 Embed (清單整合；超過字數限制自動拆訊息)。"""
    embed = NexusEmbed(
        title="📋 Nexus Seeker | 待成交委託單列表",
        description=(
            f"共 `{len(orders)}` 筆待成交委託單。\n"
            "請使用 `/remove_order` 或 `/edit_order` 指令來撤銷或微調委託單。\n\u200b"
        ),
        color=discord.Color.orange(),
        timestamp=datetime.now(timezone.utc),
    )

    if not orders:
        embed.description = "📭 目前沒有任何活躍的待成交委託單。您可以透過 `/order_panel` 建立新的掛單。"
        embed.set_footer(text="Nexus Seeker • 待成交委託單管理系統")
        return [embed]

    for idx, o in enumerate(orders):
        card_content = _build_active_order_ansi_card(o)
        embed.add_field(
            name=f"📦 委託單 #{idx+1} (ID: {o['id']})",
            value=_safe_embed_field_value(card_content, "暫無詳情"),
            inline=False,
        )

    embed.set_footer(text="Nexus Seeker • 待成交委託單管理系統")
    return split_embed_by_fields(embed)
