"""Watchlist 清單與總覽 Embed 建構函式。

包含：
- create_watchlist_embed：Watchlist 分頁清單
- create_watchlist_overview_embed：本輪 Watchlist 總覽摘要
"""

from typing import Any, Optional
import discord

from datetime import datetime, timezone
from typing import Dict, List

from cogs.embed_builders._ansi_utils import _pad_string
from cogs.embed_builders._embed_helpers import _safe_embed_field_value
from cogs.embed_builders._core import (
    NexusEmbed,
)
from database.market_cache import get_market_cache


def _classify_watchlist_cache_tag(
    spot: float, max_pain: float, em_lower: Optional[float]
) -> str:
    """依 AGENTS.md 文件化的 Local Rules Engine 門檻，將快取價相對 Max Pain 的
    偏離百分比分類為戰術標籤。刻意只依賴 `market_cache` 的輕量欄位
    (spot/max_pain/expected_move_lower)，不耦合 `/x` 全量掃描才有的 GEX/UOA 資料，
    以維持 `/list_watch` 零額外運算成本的特性。
    """
    if max_pain <= 0 or spot <= 0:
        return ""
    delta_mp_pct = (spot - max_pain) / max_pain * 100.0
    if em_lower and spot <= em_lower and delta_mp_pct < -5.0:
        return "🚀"
    if abs(delta_mp_pct) > 10.0:
        return "⚠️"
    return ""


def _format_watchlist_price_info(symbol: str) -> str:
    """讀取 `market_cache`（純本地 SQLite 快取，零額外網路呼叫）並格式化為
    「快取價 (MP偏離% 標籤)」摘要；快取不存在時回傳降級提示。"""
    cache = get_market_cache(symbol)
    if not cache or not cache.get("reference_spot_price"):
        return "-- (尚未預熱)"

    spot = float(cache["reference_spot_price"])
    max_pain = float(cache.get("max_pain") or 0.0)
    em_lower = cache.get("expected_move_lower")
    em_lower_f = float(em_lower) if em_lower is not None else None

    tag = _classify_watchlist_cache_tag(spot, max_pain, em_lower_f)
    tag_suffix = f" {tag}" if tag else ""

    if max_pain > 0:
        delta_mp_pct = (spot - max_pain) / max_pain * 100.0
        return f"${spot:.2f} (MP{delta_mp_pct:+.1f}%{tag_suffix})"
    return f"${spot:.2f}"


def create_watchlist_embed(
    page_data: Any,
    current_page: Any,
    total_pages: Any,
    total_items: Any,
    sort_label: Optional[str] = None,
    query: Optional[str] = None,
) -> discord.Embed:
    """生成觀察清單的分頁 Embed，附帶快取價與 Max Pain 偏離摘要"""

    if not page_data:
        description = (
            "目前沒有追蹤任何項目" if not query else f"沒有符合「{query}」的項目"
        )
    else:
        lines = ["```ansi"]
        header = f"{_pad_string('標的 [標籤]', 24)}{_pad_string('快取價 (MP偏離)', 20)}"
        lines.append(header)
        lines.append("-" * 44)

        for sym, tags in page_data:
            display_sym = f"{sym} [{tags}]" if tags else sym
            sym_fmt = _pad_string(display_sym, 24)
            price_info = _format_watchlist_price_info(sym)
            lines.append(f"{sym_fmt}{price_info}")

        lines.append("```")
        description = "\n".join(lines)

    filter_note_parts = []
    if sort_label:
        filter_note_parts.append(f"排序: {sort_label}")
    if query:
        filter_note_parts.append(f"搜尋: 「{query}」")
    filter_note = f"（{' ｜ '.join(filter_note_parts)}）" if filter_note_parts else ""

    embed = NexusEmbed(
        title="📡 【您的專屬觀察清單】",
        description=(
            f"目前監控中的標的清單{filter_note}。系統將每 30 分鐘自動執行量化掃描。"
            f"價格為每日盤前預熱之快取參考價，非即時報價。\n\n{description}"
        ),
        color=discord.Color.blurple(),
    )

    embed.set_footer(
        text=f"頁次: {current_page}/{total_pages} ｜ 📊 總項目: {total_items}"
    )
    return embed


def create_bulk_watchlist_result_embed(
    action_label: str,
    succeeded: List[str],
    skipped: Optional[Dict[str, List[str]]] = None,
) -> discord.Embed:
    """生成批次新增/移除觀察清單標的的結果摘要 Embed。

    Args:
        action_label: 操作名稱，例如「加入」或「移除」。
        succeeded: 成功處理的標的代號列表。
        skipped: 依原因分類、未成功處理的標的代號，例如
            {"已存在": [...], "無效代號": [...], "已達上限": [...], "找不到": [...]}。
    """
    lines: List[str] = []
    if succeeded:
        lines.append(
            f"✅ **已{action_label}** ({len(succeeded)} 檔): {', '.join(succeeded)}"
        )
    else:
        lines.append(f"⚠️ 沒有任何標的被{action_label}。")

    for reason, symbols in (skipped or {}).items():
        if symbols:
            lines.append(f"❌ **{reason}** ({len(symbols)} 檔): {', '.join(symbols)}")

    color = discord.Color.green() if succeeded else discord.Color.orange()
    embed = NexusEmbed(
        title=f"📡 觀察清單批次{action_label}結果",
        description="\n".join(lines),
        color=color,
    )
    return embed


def create_set_watchlist_result_embed(
    succeeded: List[str],
    cleared_count: int,
    invalid: Optional[List[str]] = None,
) -> discord.Embed:
    """生成覆蓋設定觀察清單標的的結果摘要 Embed。

    Args:
        succeeded: 成功設定為觀察清單的新標的代號列表。
        cleared_count: 原先被清除的舊標的總數。
        invalid: 被過濾排除的無效代號列表。
    """
    lines: List[str] = [
        f"✅ **已設定觀察清單** ({len(succeeded)} 檔): {', '.join(succeeded)}",
        f"🗑️ **已清除原清單**: {cleared_count} 檔標的",
    ]
    if invalid:
        lines.append(f"❌ **無效代號** ({len(invalid)} 檔): {', '.join(invalid)}")

    return NexusEmbed(
        title="📡 觀察清單設定結果",
        description="\n".join(lines),
        color=discord.Color.green(),
    )


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

    embed = NexusEmbed(
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
    if not overview_lines:
        embed.add_field(
            name="📋 全標的速覽",
            value=_safe_embed_field_value("", "暫無總覽"),
            inline=False,
        )
    else:
        chunk_size = 10
        total_chunks = (len(overview_lines) + chunk_size - 1) // chunk_size
        for i in range(total_chunks):
            chunk = overview_lines[i * chunk_size : (i + 1) * chunk_size]
            field_name = (
                "📋 全標的速覽"
                if total_chunks == 1
                else f"📋 全標的速覽 ({i+1}/{total_chunks})"
            )
            embed.add_field(
                name=field_name,
                value=_safe_embed_field_value("\n".join(chunk), "暫無總覽"),
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
