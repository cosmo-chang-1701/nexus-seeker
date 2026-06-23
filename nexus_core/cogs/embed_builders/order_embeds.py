"""委託單管理、盤中掃描與盤前/盤後報告 Embed 建構函式。

包含：
- create_intraday_scan_embed：盤中量化掃描與避險執行指南
- _build_active_order_ansi_card：單筆委託單 ANSI 卡片
- create_active_order_card_embed：單筆委託單卡片 Embed
- create_active_orders_embed：委託單列表 Embed
- _build_telemetry_alignment_ansi_card：Telemetry 對齊快照卡片
- create_telemetry_alignment_embeds：Telemetry 對齊警報（分頁）
- create_telemetry_alignment_embed：相容舊呼叫（回傳第一頁）
- build_pre_market_briefing_embed：盤前綜合宏觀與自選股報告
- _parse_post_market_ai_commentary：盤後 AI 段落解析
- _format_to_target_center_style：AI 文字格式化
- _format_to_target_center_style_with_title：含標題的 AI 文字格式化
- build_post_market_intelligence_embed：盤後綜合風險與 AI 策略報告
"""

import re
import discord

from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from cogs.embed_builders._ansi_utils import _pad_string, _clean_ansi, _safe_float
from cogs.embed_builders._ansi_utils import (
    _is_macro_report_marker,
    _chunk_text_blocks,
)
from cogs.embed_builders._embed_helpers import (
    _safe_embed_field_value,
    _parse_and_format_positions_table,
    split_embed_by_fields,
)


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
    """建構待成交委託單列表報告 Embed (清單整合；超過字數限制自動拆訊息)。"""
    embed = discord.Embed(
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


def _build_telemetry_alignment_ansi_card(item: Dict[str, Any]) -> str:
    """建構 Telemetry 實時對齊快照卡片。"""
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
    lines.append(
        f"\u001b[1;35m🌌 Nexus Seeker • Telemetry 實時對齊快照 [{sym}]{degraded_suffix}\u001b[0m"
    )
    lines.append(" -----------------------------------------------------------------")
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
    return "\n".join(lines)


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

    title = "🌌 Nexus Seeker | 待成交委託單實時對齊快照"
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
        embed = discord.Embed(
            title=title,
            description="\n".join(description_lines) + "\n\u200b",
            color=color,
            timestamp=datetime.now(timezone.utc),
        )

        for idx, item in enumerate(chunk):
            global_idx = chunk_idx * 15 + idx + 1
            sym = str(item.get("symbol") or "").upper()
            order_id = item.get("order_id")
            card_content = _build_telemetry_alignment_ansi_card(item)

            name_prefix = f"📦 委託單 #{global_idx}"
            if order_id is not None:
                name_prefix += f" (ID: {order_id})"

            if sym:
                field_name = f"{name_prefix} ｜ {sym}"
            else:
                field_name = name_prefix

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
    return embeds[0] if embeds else discord.Embed(title="Telemetry 對齊警報")


def build_pre_market_briefing_embed(
    macro_data: dict,
    alerts: Optional[List[Any]] = None,
    earnings_alerts: Optional[List[Dict[str, Any]]] = None,
    scanned_symbols: Optional[List[str]] = None,
    warning_days: int = 2,
) -> discord.Embed:
    """建立盤前綜合宏觀與自選股報告 Embed (🌅 盤前綜合宏觀與自選股報告)"""
    is_risky = bool(alerts) or bool(earnings_alerts)
    base_color = discord.Color.red() if is_risky else discord.Color.blue()

    embed = discord.Embed(
        title="🌅 Nexus Seeker | 盤前綜合宏觀與自選股報告",
        description="盤前 30 分鐘市場狀態與自選股財報季預警快速簡報。",
        color=base_color,
        timestamp=datetime.now(timezone.utc),
    )

    # 1. 巨觀數據指標
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
    lines.append(
        f"{_pad_string('US2Y 2Y 公債', widths[0])} | {_pad_string(f'{us2y:.2f}%', widths[1], 'right')} | {_pad_string(f'利差 {spread:+.2f}%', widths[2], 'right')}"
    )
    vix_color_start = " [0;31m" if vix > 25 else (" [0;33m" if vix > 20 else " [0;32m")
    vix_note = f"{vix_change:+.2f} ({vix_emoji})"
    vix_note_colored = f"{vix_change:+.2f} ({vix_color_start}{vix_emoji} [0m)"
    vix_val_str = f"{vix:.2f}"
    lines.append(
        f"{_pad_string('VIX 恐慌指數', widths[0])} | {_pad_string(vix_val_str, widths[1], 'right')} | {_pad_string(vix_note, widths[2], 'right').replace(vix_note, vix_note_colored)}"
    )
    lines.append("```")

    embed.add_field(name="🌍 巨觀數據指標", value="\n".join(lines), inline=False)

    # 2. 宏觀風險警示
    if alerts:
        alert_text = "\n".join([f"• {a}" for a in alerts])
        embed.add_field(
            name="🚨 宏觀風險警示 (Macro Alerts)", value=alert_text, inline=False
        )
    else:
        embed.add_field(
            name="✅ 宏觀狀態",
            value="殖利率曲線、匯率與波動率未見極端異常。維持市場部位。",
            inline=False,
        )

    # 3. 自選股財報雷達
    if earnings_alerts:
        earnings_text = "\n\n".join(
            (
                f"**{item['symbol']}** "
                f"({'⚠️ **持倉高風險**' if item['is_portfolio'] else '👀 觀察清單'})\n"
                f"└ 📅 財報日: `{item['earnings_date']}` (倒數 **{item['days_left']}** 天)"
            )
            for item in earnings_alerts
        )
        embed.add_field(
            name="🚨 自選股財報季雷達預警 (Earnings Radar)",
            value=earnings_text,
            inline=False,
        )
    else:
        scanned_list = (
            "、".join(f"`{symbol}`" for symbol in scanned_symbols)
            if scanned_symbols
            else "無"
        )
        embed.add_field(
            name="✅ 自選股財報季雷達",
            value=f"已掃描標的：{scanned_list}\n\n近 {warning_days} 日內無財報風險，安全過關！",
            inline=False,
        )

    embed.set_footer(text="🌌 Nexus Seeker • 盤前綜合簡報")
    return embed


def _parse_post_market_ai_commentary(ai_commentary: str) -> dict[str, str]:
    patterns = {
        "market": r"\*?\*?(?:1\.\s*)?📊\s*多空大盤交叉驗證解讀\*?\*?",
        "risk": r"\*?\*?(?:2\.\s*)?⚠️\s*潛在陷阱與風險提示\*?\*?",
        "strategy": r"\*?\*?(?:3\.\s*)?🛡️\s*高勝率交易策略推薦\*?\*?",
    }

    indices = {}
    for key, pattern in patterns.items():
        match = re.search(pattern, ai_commentary)
        if match:
            indices[key] = (match.start(), match.end())

    if not indices:
        return {}

    sorted_keys = sorted(indices.keys(), key=lambda k: indices[k][0])
    result = {}
    for i, key in enumerate(sorted_keys):
        start_idx = indices[key][1]
        if i + 1 < len(sorted_keys):
            next_key = sorted_keys[i + 1]
            end_idx = indices[next_key][0]
            content = ai_commentary[start_idx:end_idx].strip()
        else:
            content = ai_commentary[start_idx:].strip()

        content = re.sub(r"^[:：\s\-\*]+", "", content).strip()
        result[key] = content

    return result


def _format_to_target_center_style(text: str) -> str:
    if not text:
        return ""

    raw_lines = text.split("\n")
    cleaned_lines = []

    for line in raw_lines:
        line_str = line.strip()
        if not line_str:
            continue

        cleaned = re.sub(r"^[\-\*\•\d+\.\s]+", "", line_str).strip()
        cleaned = _clean_ansi(cleaned)
        if cleaned:
            cleaned_lines.append(cleaned)

    if not cleaned_lines:
        return text

    formatted_lines = []
    for idx, line in enumerate(cleaned_lines):
        prefix = " ├─ " if idx < len(cleaned_lines) - 1 else " └─ "
        formatted_lines.append(f"{prefix}{line}")

    return "\n".join(formatted_lines)


def _format_to_target_center_style_with_title(title: str, text: str) -> str:
    if not text:
        return "```ansi\n └─ 暫無數據\n```"

    raw_lines = text.split("\n")
    cleaned_lines = []

    for line in raw_lines:
        line_str = line.strip()
        if not line_str:
            continue

        cleaned = re.sub(r"^[\-\*\•\d+\.\s]+", "", line_str).strip()
        cleaned = _clean_ansi(cleaned)
        if cleaned:
            cleaned_lines.append(cleaned)

    if not cleaned_lines:
        return "```ansi\n └─ 暫無數據\n```"

    formatted_lines = [f" {title}"]
    for idx, line in enumerate(cleaned_lines):
        prefix = " ├─ " if idx < len(cleaned_lines) - 1 else " └─ "
        formatted_lines.append(f"{prefix}{line}")

    content = "\n".join(formatted_lines)
    return f"```ansi\n{content}\n```"


def build_post_market_intelligence_embed(
    report_lines: List[str],
    hedge_analysis: Optional[Dict[str, Any]] = None,
    survival_runway: Optional[float] = None,
    sectors_data: Optional[List[Dict[str, Any]]] = None,
    ai_commentary: Optional[str] = None,
) -> discord.Embed:
    """建立盤後綜合風險與 AI 策略報告 Embed (📋 盤後綜合風險與 AI 策略報告)"""
    embed_color = discord.Color.blue()
    if ai_commentary:
        if "🚨" in ai_commentary or "🆘" in ai_commentary:
            embed_color = discord.Color.red()
        elif "⚠️" in ai_commentary:
            embed_color = discord.Color.orange()

    embed = discord.Embed(
        title="📋 Nexus Seeker | 盤後綜合風險與 AI 策略報告",
        description="每日收盤結算、行業資金輪動、AI attribution 歸因與次日對沖決策綜合簡報。",
        color=embed_color,
        timestamp=datetime.now(timezone.utc),
    )

    if survival_runway is not None:
        runway_text = (
            "無限 (收益已覆蓋支出)"
            if survival_runway >= 9999
            else f"{survival_runway:,.1f} 天"
        )
        runway_ansi = (
            "```ansi\n"
            f" ├─ 預估剩餘天數: \u001b[1;36m{runway_text}\u001b[0m\n"
            " └─ 計算基準: 基於現有現金儲備與 Theta 收益\n"
            "```"
        )
        embed.add_field(
            name="🏁 財務生存跑道 (Financial Runway)",
            value=runway_ansi,
            inline=False,
        )

    positions_list = []
    debit_cost_val = "$0.00 USD"
    credit_cash_val = "$0.00 USD"
    pnl_val_str = "$0.00 USD"

    if report_lines:
        macro_index = -1
        for i, line in enumerate(report_lines):
            if _is_macro_report_marker(line):
                macro_index = i
                break
        if macro_index != -1:
            positions_list = [
                line.strip() for line in report_lines[:macro_index] if line.strip()
            ]
            macro_text = "\n".join(
                line.strip() for line in report_lines[macro_index:] if line.strip()
            )
        else:
            positions_list = [line.strip() for line in report_lines if line.strip()]
            macro_text = "目前無宏觀風險數據。"
        if positions_list:
            positions_text = _parse_and_format_positions_table(
                positions_list, survival_runway
            )
        else:
            positions_text = "目前無持倉部位。"
    else:
        positions_text = "目前無持倉部位。"
        macro_text = "目前無宏觀風險數據。"

    if (
        macro_text
        and macro_text.strip()
        and macro_text.strip() != "目前無宏觀風險數據。"
    ):
        macro_lines = macro_text.split("\n")
        cleaned_macro = [line.strip() for line in macro_lines if line.strip()]
        formatted_macro_lines = []
        for idx, line in enumerate(cleaned_macro):
            clean_line = re.sub(r"^[\-\*\•\s]+", "", line).strip()
            clean_line = clean_line.replace("`", "").replace("*", "")
            prefix = " ├─ " if idx < len(cleaned_macro) - 1 else " └─ "

            # Apply color to key indicators
            if "Beta-Weighted Delta" in clean_line or "曝險" in clean_line:
                clean_line = f"\u001b[0;33m{clean_line}\u001b[0m"
            elif "警告" in clean_line or "危險" in clean_line or "🚨" in clean_line:
                clean_line = f"\u001b[0;31m{clean_line}\u001b[0m"

            formatted_macro_lines.append(f"{prefix}{clean_line}")
        macro_content = "\n".join(formatted_macro_lines)
        macro_value = f"```ansi\n{macro_content}\n```"
    else:
        macro_value = "```ansi\n └─ 目前無宏觀風險數據。\n```"

    # Process positions text to extract financial summary and chunk positions
    if positions_list and positions_text and positions_text != "目前無持倉部位。":
        if "財務摘要 (Financial Summary)" in positions_text:
            table_part, summary_part = positions_text.split(
                "財務摘要 (Financial Summary)"
            )
            summary_text = "財務摘要 (Financial Summary)" + summary_part
            debit_match = re.search(r"Debit Cost.*:\s*(.*)", summary_text)
            credit_match = re.search(r"Credit Cash.*:\s*(.*)", summary_text)
            pnl_match = re.search(r"Unrealized PnL.*:\s*(.*)", summary_text)

            debit_cost_val = (
                _clean_ansi(debit_match.group(1).strip())
                if debit_match
                else "$0.00 USD"
            )
            credit_cash_val = (
                _clean_ansi(credit_match.group(1).strip())
                if credit_match
                else "$0.00 USD"
            )
            pnl_val_str = (
                _clean_ansi(pnl_match.group(1).strip()) if pnl_match else "$0.00 USD"
            )

            positions_text = table_part.strip()

        positions_text = positions_text.strip().strip("`").strip()
        blocks = [b.strip() for b in positions_text.split("\n\n") if b.strip()]

        transformed_blocks = []
        for block in blocks:
            lines = [line.strip() for line in block.split("\n") if line.strip()]
            if not lines:
                continue
            heading = lines[0]
            heading_clean = heading.replace("**", "").replace("🔹 ", "").strip()
            heading_colored = f"\u001b[1;36m{heading_clean}\u001b[0m"

            detail_lines = []
            for line in lines[1:]:
                cleaned_line = re.sub(r"^[\-\*\•\s]+", "", line).strip()
                cleaned_line = cleaned_line.replace("`", "").replace("*", "")
                detail_lines.append(cleaned_line)

            ansi_lines = [f" {heading_colored}"]
            for idx, dl in enumerate(detail_lines):
                prefix = " ├─ " if idx < len(detail_lines) - 1 else " └─ "
                if "損益：" in dl:
                    if "🟢" in dl or "+" in dl:
                        dl = f"\u001b[0;32m{dl}\u001b[0m"
                    elif "🚨" in dl or "🔴" in dl or "-" in dl:
                        dl = f"\u001b[0;31m{dl}\u001b[0m"
                ansi_lines.append(f"{prefix}{dl}")

            transformed_block = "\n".join(ansi_lines)
            transformed_blocks.append(transformed_block)

        chunks = _chunk_text_blocks(transformed_blocks, max_len=1000)

        for i, chunk in enumerate(chunks):
            name = (
                f"📊 投資組合收盤持倉明細 ({i+1}/{len(chunks)})"
                if len(chunks) > 1
                else "📊 投資組合收盤持倉明細"
            )
            embed.add_field(name=name, value=f"```ansi\n{chunk}\n```", inline=False)

        # Strip markdown bold & codeblock residues from summary strings before ANSI wrapping
        debit_cost_clean = debit_cost_val.replace("`", "").replace("**", "").strip()
        credit_cash_clean = credit_cash_val.replace("`", "").replace("**", "").strip()
        pnl_val_clean = pnl_val_str.replace("`", "").replace("**", "").strip()

        pnl_color = ""
        if "🟢" in pnl_val_clean or "+" in pnl_val_clean:
            pnl_color = "\u001b[0;32m"
        elif "🚨" in pnl_val_clean or "🔴" in pnl_val_clean or "-" in pnl_val_clean:
            pnl_color = "\u001b[0;31m"

        pnl_val_ansi = (
            f"```ansi\n{pnl_color}{pnl_val_clean}\u001b[0m\n```"
            if pnl_color
            else f"```ansi\n{pnl_val_clean}\n```"
        )

        # Add inline fields for financial summary
        embed.add_field(
            name="💰 實質暴露 (Debit Cost)",
            value=f"```ansi\n{debit_cost_clean}\n```",
            inline=True,
        )
        embed.add_field(
            name="💵 收取權利金 (Credit Cash)",
            value=f"```ansi\n{credit_cash_clean}\n```",
            inline=True,
        )
        embed.add_field(
            name="📊 未實現損益 (Unrealized PnL)", value=pnl_val_ansi, inline=True
        )
    else:
        embed.add_field(
            name="📊 投資組合收盤持倉明細",
            value="```ansi\n └─ 目前無持倉部位。\n```",
            inline=False,
        )

    embed.add_field(
        name="🌐 投資組合收盤宏觀風險",
        value=_safe_embed_field_value(macro_value, "無數據"),
        inline=False,
    )

    if sectors_data is not None:
        if sectors_data:
            sorted_sectors = sorted(
                sectors_data,
                key=lambda item: abs(_safe_float(item.get("pct_change"))),
                reverse=True,
            )
            sector_content_lines = []
            for idx, item in enumerate(sorted_sectors):
                symbol = item.get("symbol", "N/A")
                name = item.get("name", "N/A")
                change = _safe_float(item.get("pct_change"))
                rel_vol = _safe_float(item.get("rel_vol"))
                skew = _safe_float(item.get("skew"))
                uoa_count = int(item.get("uoa_count", 0))

                change_emoji = "🟢" if change > 0 else "🚨" if change < 0 else "⚖️"
                color_code = (
                    "\u001b[0;32m"
                    if change > 0
                    else "\u001b[0;31m"
                    if change < 0
                    else "\u001b[0;37m"
                )
                reset_code = "\u001b[0m"

                prefix = " ├─ " if idx < len(sorted_sectors) - 1 else " └─ "
                sector_content_lines.append(
                    f"{prefix}{symbol} ({name})：{color_code}{change_emoji} {change:+.2f}%{reset_code} ｜ 量比 {rel_vol:.2f}x ｜ Skew {skew:+.1f} ｜ UOA {uoa_count}"
                )
            sector_content = "\n".join(sector_content_lines)
            sector_value = f"```ansi\n{sector_content}\n```"
        else:
            sector_value = "```ansi\n └─ 暫無行業資金輪動數據。\n```"

        embed.add_field(
            name="🔄 行業板塊資金輪動 (Sector Rotation)",
            value=_safe_embed_field_value(sector_value, "無數據"),
            inline=False,
        )

    if ai_commentary:
        parsed = _parse_post_market_ai_commentary(ai_commentary)
        if parsed:
            if parsed.get("market"):
                embed.add_field(
                    name="📊 AI 多空大盤交叉驗證解讀",
                    value=_safe_embed_field_value(
                        _format_to_target_center_style_with_title(
                            "多空大盤交叉驗證 (Market Cross-Validation)",
                            parsed["market"],
                        ),
                        "暫無分析",
                    ),
                    inline=False,
                )
            if parsed.get("risk"):
                embed.add_field(
                    name="⚠️ AI 潛在陷阱與風險提示",
                    value=_safe_embed_field_value(
                        _format_to_target_center_style_with_title(
                            "潛在陷阱與風險提示 (Risk Warning & Trap Alert)",
                            parsed["risk"],
                        ),
                        "暫無分析",
                    ),
                    inline=False,
                )
            if parsed.get("strategy"):
                embed.add_field(
                    name="🛡️ AI 高勝率交易策略推薦",
                    value=_safe_embed_field_value(
                        _format_to_target_center_style_with_title(
                            "高勝率交易策略推薦 (Recommended Trading Strategies)",
                            parsed["strategy"],
                        ),
                        "暫無分析",
                    ),
                    inline=False,
                )
        else:
            embed.add_field(
                name="🧠 AI 損益歸因與次日策略點評",
                value=_safe_embed_field_value(
                    _format_to_target_center_style_with_title(
                        "AI 損益歸因與次日策略 (AI Attribution & Strategy)",
                        ai_commentary,
                    ),
                    "暫無分析",
                ),
                inline=False,
            )

    embed.set_footer(text="🌌 Nexus Seeker • 盤後綜合策略簡報")
    return embed
