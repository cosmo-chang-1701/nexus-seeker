from __future__ import annotations

from typing import Mapping

from cogs.embed_builder import _pad_string, _visual_len
from models.schemas import (
    EnhancedWatchlistMetrics,
    WatchlistTacticalPlan,
)

C_RESET = "\u001b[0m"
C_GREEN = "\u001b[1;32m"
C_YELLOW = "\u001b[1;33m"
C_RED = "\u001b[1;31m"
C_CYAN = "\u001b[1;36m"


def _wrap_visual(text: str, width: int, indent: str = "") -> list[str]:
    lines: list[str] = []
    current = ""
    for char in text:
        candidate = current + char
        if current and _visual_len(candidate) > width:
            lines.append(current)
            current = indent + char
        else:
            current = candidate
    if current:
        lines.append(current)
    return lines or [indent]


def _format_pair(
    left_label: str,
    left_value: str,
    right_label: str,
    right_value: str,
    *,
    left_color: str = "",
    right_color: str = "",
) -> str:
    left_prefix = _pad_string(f"{left_label} ", 12)
    right_prefix = _pad_string(f"{right_label} ", 12)
    left_plain = f"{left_prefix}{left_value}"
    right_plain = f"{right_prefix}{right_value}"
    left_cell = _pad_string(left_plain, 37)
    if left_color:
        left_cell = left_cell.replace(
            left_value, f"{left_color}{left_value}{C_RESET}", 1
        )
    right_cell = right_plain
    if right_color:
        right_cell = right_cell.replace(
            right_value, f"{right_color}{right_value}{C_RESET}", 1
        )
    return f"{left_cell}{right_cell}"


def _format_single(label: str, value: str, *, color: str = "") -> str:
    prefix = _pad_string(f"{label} ", 12)
    if color:
        return f"{prefix}{color}{value}{C_RESET}"
    return f"{prefix}{value}"


def generate_ansi_watchlist_report(
    metrics: EnhancedWatchlistMetrics,
    tactical: Mapping[str, object] | WatchlistTacticalPlan,
) -> str:
    tactical_model = (
        tactical
        if isinstance(tactical, WatchlistTacticalPlan)
        else WatchlistTacticalPlan.model_validate(tactical)
    )

    if tactical_model.alert_level == "red":
        route_color = C_RED
    elif tactical_model.alert_level == "yellow":
        route_color = C_YELLOW
    else:
        route_color = C_GREEN

    if metrics.current_price <= metrics.buy_price_phase2:
        price_color = C_RED
    elif metrics.current_price <= metrics.buy_price_phase1:
        price_color = C_YELLOW
    else:
        price_color = C_GREEN

    bias_color = C_GREEN if metrics.bias_ma20 >= 0 else C_RED
    support_color = (
        C_RED
        if metrics.distance_to_absolute_support <= 0
        else C_YELLOW
        if metrics.distance_to_absolute_support <= 0.03
        else C_GREEN
    )
    hidden_delta_color = (
        C_RED if abs(tactical_model.hidden_delta_risk) >= 25 else C_YELLOW
    )
    rs_color = C_GREEN if metrics.relative_strength_spy >= 0 else C_RED
    skew_color = (
        C_RED
        if metrics.option_skew >= 5
        else C_GREEN
        if metrics.option_skew <= -2
        else C_YELLOW
    )

    # 1. 標題與基本快照
    lines = [
        "```ansi",
        f" 📊 {metrics.symbol} | {metrics.exchange} 技術與期權快照",
        " ----------------------------------",
        "  技術面與現價快照 (Technical & Price Spot)",
        f"  ├─ 現價: {price_color}${metrics.current_price:.2f}{C_RESET} | PE: {metrics.pe_ratio or 'N/A'} | Beta: {metrics.beta:.2f}",
        f"  ├─ RSI 14: {C_YELLOW if metrics.rsi_14 > 65 else C_GREEN}{metrics.rsi_14:.1f}{C_RESET} | ATR 14: {metrics.atr_14:.2f} | Option Skew: {skew_color}{metrics.option_skew:+.2f}%{C_RESET}",
        f"  ├─ MA200 支撐: ${metrics.ma200:.2f} | 相對 SPY: {rs_color}{metrics.relative_strength_spy * 100:+.2f}%{C_RESET}",
        f"  └─ 均線乖離: MA20: ${metrics.ma20:.2f} / MA50: ${metrics.ma50:.2f} | MA20 偏離: {bias_color}{metrics.bias_ma20 * 100:+.2f}%{C_RESET}",
        " ----------------------------------",
        "  🛡️ 技術 / 防禦牆 (Technical & Defense Walls)",
        f"  ├─ 狀態判讀: 買點狀態: {metrics.buy_zone_status} | 賣出狀態: {metrics.sell_zone_status} | 絕對支撐距: {support_color}{metrics.distance_to_absolute_support * 100:+.2f}%{C_RESET}",
        f"  ├─ 買點支撐: P1: ${metrics.buy_price_phase1:.2f} | P2: ${metrics.buy_price_phase2:.2f} | P3: ${metrics.buy_price_phase3:.2f}",
        f"  ├─ 賣出阻力: P1: ${metrics.sell_price_phase1:.2f} | P2: ${metrics.sell_price_phase2:.2f} | P3: ${metrics.sell_price_phase3:.2f}",
        f"  └─ 關鍵位與敏感度: Vol POC: ${metrics.volume_poc:.2f} | GEX PutWall: ${metrics.gex_max_put_wall:.2f} | Vanna: {metrics.vanna_sensitivity:+.4f}",
        " ----------------------------------",
        "  ⚙️ SDDM / 對沖 (SDDM Routing & Hedge Control)",
        f"  ├─ 路由機制: {route_color}{tactical_model.sddm_route}{C_RESET} | 網格步長: {tactical_model.dynamic_grid_step:.2f}",
        f"  ├─ Delta 曝險: Hidden Δ: {hidden_delta_color}{tactical_model.hidden_delta_risk:+.2f}{C_RESET} | 對沖股數: {C_RED if tactical_model.hedge_allocation_shares > 0 else C_GREEN}{tactical_model.hedge_allocation_shares}{C_RESET} 股",
    ]

    instruction_lines = _wrap_visual(
        tactical_model.action_guideline,
        width=50,
        indent=" " * 14,
    )
    lines.append(f"  ├─ 執行指南: {route_color}{instruction_lines[0]}{C_RESET}")
    for extra_line in instruction_lines[1:]:
        lines.append(f"  │            {route_color}{extra_line}{C_RESET}")

    if tactical_model.hedge_instruction:
        hedge_lines = _wrap_visual(
            tactical_model.hedge_instruction,
            width=50,
            indent=" " * 14,
        )
        lines.append(f"  └─ 對沖指令: {C_RED}{hedge_lines[0]}{C_RESET}")
        for extra_line in hedge_lines[1:]:
            lines.append(f"               {C_RED}{extra_line}{C_RESET}")
    else:
        # If no hedge instruction, make execution guideline the leaf node
        lines[-1] = lines[-1].replace("  ├─ 執行指南:", "  └─ 執行指南:")

    lines.append("```")
    return "\n".join(lines)
