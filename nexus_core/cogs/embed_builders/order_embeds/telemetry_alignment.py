"""Telemetry 委託單實時對齊警報 Embed 建構函式。"""

import re
from datetime import datetime, timezone
from typing import Any, Dict, List, Tuple

import discord

from cogs.embed_builders._core import NexusEmbed
from cogs.embed_builders._embed_helpers import (
    _safe_embed_field_value,
    split_embed_by_fields,
)


def _build_telemetry_alignment_ansi_card(item: Dict[str, Any]) -> Tuple[str, str]:
    """建構 Telemetry 實時對齊快照卡片。回傳 (卡片內容, 數據降級狀態後綴)。"""
    sym = str(item.get("symbol") or "").upper()
    current_price = float(item.get("live_price") or item.get("current_price") or 0.0)

    avg_cost_val = item.get("avg_cost")
    avg_cost = float(avg_cost_val) if avg_cost_val is not None else None

    gain_loss_pct_val = item.get("gain_loss_pct")
    gain_loss_pct = float(gain_loss_pct_val) if gain_loss_pct_val is not None else None

    put_wall_val = item.get("put_wall")
    put_wall = (
        float(put_wall_val)
        if put_wall_val is not None and float(put_wall_val) > 0.0
        else None
    )

    wall_dist_pct_val = item.get("wall_dist_pct")
    wall_dist_pct = float(wall_dist_pct_val) if wall_dist_pct_val is not None else None

    order_id = int(item.get("order_id") or 0)
    limit_price = float(item.get("current_price") or 0.0)
    shares = int(round(float(item.get("original_qty") or 0.0)))
    proximity_pct = float(item.get("proximity_pct") or 0.0)

    skew_val_val = item.get("skew_val")
    skew_val = float(skew_val_val) if skew_val_val is not None else None

    skew_pct_val = item.get("skew_pct")
    skew_pct = float(skew_pct_val) if skew_pct_val is not None else None

    iv_val_val = item.get("iv_val")
    iv_val = float(iv_val_val) if iv_val_val is not None else None

    iv_rank_val = item.get("iv_rank")
    iv_rank = float(iv_rank_val) if iv_rank_val is not None else None

    # Format output strings
    avg_cost_str = f"${avg_cost:.2f}" if avg_cost is not None else "N/A"
    gain_loss_pct_str = f"{gain_loss_pct:+.2f}%" if gain_loss_pct is not None else "--%"
    put_wall_str = f"${put_wall:.2f}" if put_wall is not None else "N/A"
    wall_dist_pct_str = (
        f"{wall_dist_pct:+.2f}%"
        if wall_dist_pct is not None and put_wall is not None
        else "--%"
    )

    skew_val_str = f"{skew_val:+.2f}%" if skew_val is not None else "--%"
    skew_pct_str = f"{skew_pct:.2f}%" if skew_pct is not None else "--%"

    # Fallback/Degradation metrics check
    is_premarket = item.get("is_premarket", False)
    iv_source = item.get("iv_source", "UNAVAILABLE")

    is_degraded = (
        put_wall is None
        or skew_val is None
        or iv_val is None
        or item.get("iv_status") == "UNAVAILABLE"
        or item.get("skew_status") == "ERROR"
        or item.get("skew_status") == "N/A"
    )

    if is_premarket:
        if iv_val is None or iv_val == 0.0 or iv_source == "UNAVAILABLE":
            degraded_suffix = " [盤前數據未更新]"
            iv_val_str = "--%"
            iv_rank_str = "--%"
            iv_status = "等待開盤"
        elif iv_source == "STORED_IV":
            degraded_suffix = " [盤前/前日收盤]"
            iv_val_str = f"{iv_val:.2f}% (前日收盤)" if iv_val is not None else "--%"
            iv_rank_str = f"{iv_rank:.2f}% (前日收盤)" if iv_rank is not None else "--%"
            iv_status = item.get("iv_status", "Normal")
        elif iv_source == "HV_PROXY":
            degraded_suffix = " [盤前/HV代理]"
            iv_val_str = (
                f"{iv_val:.2f}% (歷史波動率代理)" if iv_val is not None else "--%"
            )
            iv_rank_str = (
                f"{iv_rank:.2f}% (歷史波動率代理)" if iv_rank is not None else "--%"
            )
            iv_status = item.get("iv_status", "Normal")
        else:
            degraded_suffix = " [盤前數據降級]"
            iv_val_str = f"{iv_val:.2f}%" if iv_val is not None else "--%"
            iv_rank_str = f"{iv_rank:.2f}%" if iv_rank is not None else "--%"
            iv_status = item.get("iv_status", "Normal")
    else:
        degraded_suffix = " [數據未更新/降級模式]" if is_degraded else ""
        iv_val_str = f"{iv_val:.2f}%" if iv_val is not None else "--%"
        iv_rank_str = f"{iv_rank:.2f}%" if iv_rank is not None else "--%"
        iv_status = item.get("iv_status", "Normal")

    # ANSI Coloring values
    holding_shares = int(item.get("holding_shares", 0))
    holding_status = item.get("holding_status", "待確認")
    if "持倉" in holding_status or "ACTIVE" in holding_status:
        holding_status_color = f"\u001b[1;32m{holding_status}\u001b[0m"
    elif "空倉" in holding_status:
        holding_status_color = f"\u001b[1;30m{holding_status}\u001b[0m"
    else:
        holding_status_color = f"\u001b[1;33m{holding_status}\u001b[0m"

    holding_type_label = item.get("holding_type_label", "LEVERAGED")
    holding_type_color = f"\u001b[1;36m{holding_type_label}\u001b[0m"

    if gain_loss_pct is not None:
        if gain_loss_pct > 0.0:
            pnl_color = f"\u001b[1;32m{gain_loss_pct_str}\u001b[0m"
        elif gain_loss_pct < 0.0:
            pnl_color = f"\u001b[1;31m{gain_loss_pct_str}\u001b[0m"
        else:
            pnl_color = f"\u001b[1;30m{gain_loss_pct_str}\u001b[0m"
    else:
        pnl_color = "\u001b[1;30m--%\u001b[0m"

    wall_status = item.get("wall_status", "待確認")
    if "跌破" in wall_status or "BREAK" in wall_status or "CRITICAL" in wall_status:
        wall_status_color = f"\u001b[1;31m{wall_status}\u001b[0m"
    elif "上方" in wall_status or "SAFE" in wall_status or "BUFFER" in wall_status:
        wall_status_color = f"\u001b[1;32m{wall_status}\u001b[0m"
    else:
        wall_status_color = f"\u001b[1;33m{wall_status}\u001b[0m"

    skew_status = item.get("skew_status", "平穩")
    if (
        "極端" in skew_status
        or "警告" in skew_status
        or "ERROR" in skew_status
        or "下行" in skew_status
    ):
        skew_status_color = f"\u001b[1;31m{skew_status}\u001b[0m"
    elif "平穩" in skew_status or "Normal" in skew_status:
        skew_status_color = f"\u001b[1;32m{skew_status}\u001b[0m"
    else:
        skew_status_color = f"\u001b[1;33m{skew_status}\u001b[0m"

    if "等待開盤" in iv_status:
        iv_status_color = f"\u001b[1;33m{iv_status}\u001b[0m"
    elif "UNAVAILABLE" in iv_status or "ERROR" in iv_status:
        iv_status_color = f"\u001b[1;31m{iv_status}\u001b[0m"
    elif (
        "HIGH" in iv_status.upper()
        or "OVERHEATED" in iv_status.upper()
        or "泡沫" in iv_status
    ):
        iv_status_color = f"\u001b[1;31m{iv_status}\u001b[0m"
    else:
        iv_status_color = f"\u001b[1;32m{iv_status}\u001b[0m"

    if iv_rank is not None:
        if iv_rank >= 80.0:
            iv_rank_str_color = f"\u001b[1;31m{iv_rank_str}\u001b[0m"
        elif iv_rank >= 50.0:
            iv_rank_str_color = f"\u001b[1;33m{iv_rank_str}\u001b[0m"
        else:
            iv_rank_str_color = f"\u001b[1;32m{iv_rank_str}\u001b[0m"
    else:
        iv_rank_str_color = f"\u001b[1;30m{iv_rank_str}\u001b[0m"

    side = str(item.get("side") or "BUY").upper()
    side_zh = "買入" if side == "BUY" else "賣出"
    side_color = "\u001b[1;32m" if side == "BUY" else "\u001b[1;31m"
    order_type = str(item.get("order_type") or "LIMIT").upper()
    type_zh = "限價" if "LIMIT" in order_type else "停損"

    radar_status = item.get("radar_status", "監控中")
    if "鎖定" in radar_status or "LOCKED" in radar_status:
        radar_status_color = f"\u001b[1;32m{radar_status}\u001b[0m"
    elif "偏離" in radar_status or "SUPPRESSED" in radar_status:
        radar_status_color = f"\u001b[1;31m{radar_status}\u001b[0m"
    else:
        radar_status_color = f"\u001b[1;33m{radar_status}\u001b[0m"

    system_status_flag = item.get("system_status_flag", "TELEMETRY ACTIVE")
    if "ACTIVE" in system_status_flag or "RUNNING" in system_status_flag:
        sys_flag_color = f"\u001b[1;32m{system_status_flag}\u001b[0m"
    elif "LOCKED" in system_status_flag or "SUPPRESSED" in system_status_flag:
        sys_flag_color = f"\u001b[1;31m{system_status_flag}\u001b[0m"
    else:
        sys_flag_color = f"\u001b[1;33m{system_status_flag}\u001b[0m"

    system_instruction_directive = item.get(
        "system_instruction_directive", "通過實時防線，維持紀律掛單。"
    )
    sys_dir_color = f"\u001b[1;33m{system_instruction_directive}\u001b[0m"

    lines = ["```ansi"]
    lines.append("🛡️ \u001b[1;36m【物理防線 (The Shield)】\u001b[0m")
    lines.append(
        " ├─ 持倉型態: "
        f"{holding_type_color} ｜ 持股: \u001b[1;37m{holding_shares}\u001b[0m 股 "
        f"[{holding_status_color}]"
    )
    lines.append(
        " ├─ 成本對齊: "
        f"平均成本 \u001b[1;33m{avg_cost_str}\u001b[0m ｜ 當前現價: \u001b[1;37m${current_price:.2f}\u001b[0m (損益: {pnl_color})"
    )
    lines.append(
        " └─ 做市商牆: "
        f"GEX PutWall \u001b[1;33m{put_wall_str}\u001b[0m ｜ 距離硬支撐: \u001b[1;37m{wall_dist_pct_str}\u001b[0m "
        f"({wall_status_color})"
    )
    lines.append("")
    lines.append("📐 \u001b[1;36m【籌碼偏斜 (Market Intention)】\u001b[0m")
    lines.append(
        " ├─ 選擇權偏斜 (Option Skew): "
        f"\u001b[1;37m{skew_val_str}\u001b[0m (分位點 \u001b[1;37m{skew_pct_str}\u001b[0m) ── [狀態: {skew_status_color}]"
    )
    lines.append(
        " └─ 隱含波動率 (IV): "
        f"\u001b[1;37m{iv_val_str}\u001b[0m (IV Rank: {iv_rank_str_color}) ── [狀態: {iv_status_color}]"
    )
    lines.append("")
    lines.append("⚔️ \u001b[1;36m【捕獸夾雷達 (Order Radar)】\u001b[0m")
    lines.append(
        f" └─ ID \u001b[1;33m{order_id}\u001b[0m ({sym} {side_color}{side_zh}{type_zh}\u001b[0m ${limit_price:.2f} / \u001b[1;37m{shares}\u001b[0m股) ── 距離成交差: "
        f"\u001b[1;37m{proximity_pct:.2f}%\u001b[0m [{radar_status_color}]"
    )
    lines.append("")
    lines.append("⚙️ \u001b[1;36m【最高主權指令 (Sovereign Command)】\u001b[0m")
    lines.append(f" ├─ 狀態: {sys_flag_color}")
    lines.append(f" └─ 指引: {sys_dir_color}")
    lines.append(" -----------------------------------------------------------------")
    lines.append("```")
    return "\n".join(lines), degraded_suffix


def create_telemetry_alignment_embeds(
    alignment_items: List[Dict[str, Any]],
    truncated: bool = False,
    include_apply_button_hint: bool = True,
    scheduled_mode: bool = False,
) -> List[discord.Embed]:
    """建構 Telemetry 對齊警報 (清單整合；超過字數限制自動拆訊息)。

    - 版面參照 `create_active_orders_embed()`（清單欄位式）
    - scheduled_mode=True: 盤中每半小時自動推播版本（不含按鈕）
    """

    title = "🌌 快照：待成交委託單實時對齊"
    if scheduled_mode:
        title += " (盤中每半小時)"

    color = (
        discord.Color.red()
        if any(i.get("is_size_down") for i in alignment_items)
        else discord.Color.orange()
    )

    description_lines = [f"共 `{len(alignment_items)}` 筆委託單需要進行遙測對齊。"]

    if truncated:
        description_lines.append(
            "⚠️ 由於委託單數量較多，部分標的已被安全省略；可先用 `/list_orders` 查看完整列表。"
        )

    if include_apply_button_hint and not scheduled_mode:
        description_lines.append(
            "點擊下方按鈕可一鍵套用遙測建議價（將同步調整價格與股數）。"
        )
    else:
        description_lines.append(
            "此為通知版本（不含按鈕）。若要一鍵套用，請使用 `/telemetry_alert` 開啟互動面板。"
        )

    # 實施分頁原則：每頁最多封裝 15 個標的
    chunked_groups = [
        alignment_items[i : i + 15] for i in range(0, len(alignment_items), 15)
    ]
    all_embeds = []

    for chunk_idx, chunk in enumerate(chunked_groups):
        embed = NexusEmbed(
            title=title,
            description="\n".join(description_lines) + "\n\u200b",
            color=color,
            timestamp=datetime.now(timezone.utc),
        )

        for idx, item in enumerate(chunk):
            global_idx = chunk_idx * 15 + idx + 1
            sym = str(item.get("symbol") or "").upper()
            order_id = item.get("order_id")
            card_content, degraded_suffix = _build_telemetry_alignment_ansi_card(item)

            name_prefix = f"📦 委託單 #{global_idx}"
            if order_id is not None:
                name_prefix += f" (ID: {order_id})"

            if sym:
                field_name = f"{name_prefix} ｜ {sym}{degraded_suffix}"
            else:
                field_name = f"{name_prefix}{degraded_suffix}"

            embed.add_field(
                name=field_name,
                value=_safe_embed_field_value(card_content, "暫無遙測對齊詳情"),
                inline=False,
            )

        embed.set_footer(text="Nexus Seeker • 待成交委託單管理系統")
        sub_embeds = split_embed_by_fields(embed)
        all_embeds.extend(sub_embeds)

    # 動態頁碼標記：當分頁數大於 1 時，Embed Title 尾部動態添加 ` (第 X/Y 頁)` 標記。
    if len(all_embeds) > 1:
        for idx, emb in enumerate(all_embeds, start=1):
            base_title = emb.title or ""
            # 移除 split_embed_by_fields 自帶的 "(index/total)" 尾碼
            base_title = re.sub(r"\s*\(\d+/\d+\)$", "", base_title).rstrip()
            emb.title = f"{base_title} (第 {idx}/{len(all_embeds)} 頁)"

    return all_embeds


def create_telemetry_alignment_embed(
    alignment_items: List[Dict[str, Any]],
    truncated: bool = False,
    include_apply_button_hint: bool = True,
    scheduled_mode: bool = False,
) -> discord.Embed:
    """相容舊呼叫：回傳第一頁 Embed。"""
    embeds = create_telemetry_alignment_embeds(
        alignment_items,
        truncated=truncated,
        include_apply_button_hint=include_apply_button_hint,
        scheduled_mode=scheduled_mode,
    )
    return embeds[0] if embeds else NexusEmbed(title="🌌 快照：待成交委託單實時對齊")
