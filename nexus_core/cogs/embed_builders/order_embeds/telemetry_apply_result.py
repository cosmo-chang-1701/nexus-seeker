"""Telemetry 一鍵套用結果 Embed 建構函式。"""

from typing import Any, Dict, List

import discord

from cogs.embed_builders._ansi_utils import _to_float_or_none, fmt_or_dash
from cogs.embed_builders._core import NexusEmbed

# 每頁最多容納的委託單數 (每筆一個欄位)
_ORDERS_PER_PAGE = 10

_RESET = "\u001b[0m"
_CYAN = "\u001b[1;36m"
_YELLOW = "\u001b[1;33m"
_GREEN = "\u001b[1;32m"
_WHITE = "\u001b[1;37m"
_RED = "\u001b[1;31m"


def _fmt_pct_change(old: Any, new: Any) -> str:
    old_f = _to_float_or_none(old)
    new_f = _to_float_or_none(new)
    if old_f is None or new_f is None or old_f == 0.0:
        return "--%"
    return f"{(new_f - old_f) / old_f * 100.0:+.2f}%"


def _build_apply_detail_field(detail: Dict[str, Any]) -> tuple[str, str]:
    """單筆委託單的欄位 (名稱, ANSI 內容)。缺值一律顯示 `--`。"""
    order_id = detail.get("order_id")
    symbol = str(detail.get("symbol") or "").upper() or "--"
    order_type = str(detail.get("order_type") or "").upper() or "--"
    name = f"📦 委託單 ID {order_id if order_id is not None else '--'} ｜ {symbol}"

    old_price = detail.get("old_price")
    new_price = detail.get("new_price")
    price_label = "停損價" if order_type == "STOP" else "限價"

    rows: List[str] = [
        f"類型: {_WHITE}{order_type}{_RESET}",
        (
            f"{price_label}: {_YELLOW}{fmt_or_dash(old_price, prefix='$')}{_RESET}"
            f" → {_GREEN}{fmt_or_dash(new_price, prefix='$')}{_RESET}"
            f" ({_fmt_pct_change(old_price, new_price)})"
        ),
    ]

    if order_type == "STOP_LIMIT":
        rows.append(
            f"停損腿: {_YELLOW}{fmt_or_dash(detail.get('old_stop'), prefix='$')}{_RESET}"
            f" → {_GREEN}{fmt_or_dash(detail.get('new_stop'), prefix='$')}{_RESET}"
        )

    old_qty = fmt_or_dash(detail.get("old_qty"), ",.0f")
    new_qty = fmt_or_dash(detail.get("new_qty"), ",.0f")
    qty_row = f"股數: {_YELLOW}{old_qty}{_RESET} → {_GREEN}{new_qty}{_RESET} 股"
    if detail.get("is_size_down") is True:
        qty_row += f" {_RED}[⚠️ 尾端風險防禦]{_RESET}"
    rows.append(qty_row)

    lines = ["```ansi"]
    for idx, row in enumerate(rows):
        glyph = "└─" if idx == len(rows) - 1 else "├─"
        lines.append(f" {glyph} {row}")
    lines.append("```")
    return name, "\n".join(lines)


def create_telemetry_apply_result_embed(
    updated_count: int, details: List[Dict[str, Any]]
) -> List[discord.Embed]:
    """建構一鍵套用遙測建議的結果 Embed (每筆委託單一個欄位，每頁最多 10 筆)。

    任一筆為尾端風險縮量 (size-down) 時使用特殊事件色，否則使用成功色。
    """
    any_size_down = any(d.get("is_size_down") is True for d in details)
    color = discord.Color(0x8E44AD if any_size_down else 0x2ECC71)

    pages = [
        details[i : i + _ORDERS_PER_PAGE]
        for i in range(0, len(details), _ORDERS_PER_PAGE)
    ] or [[]]

    embeds: List[discord.Embed] = []
    for page_idx, page in enumerate(pages, start=1):
        title = "✅ 動態遙測對齊完成"
        if len(pages) > 1:
            title += f" (第 {page_idx}/{len(pages)} 頁)"
        embed = NexusEmbed(
            title=title,
            description=(
                f"已成功更新 `{int(updated_count)}` 筆待成交委託單，"
                "價格與股數已依快照中的遙測建議調整。"
            ),
            color=color,
        )
        for detail in page:
            name, value = _build_apply_detail_field(detail)
            embed.add_field(name=name, value=value, inline=False)
        embed.set_footer(text="遙測對齊套用結果")
        embeds.append(embed)
    return embeds
