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

    lines = [
        "```ansi",
        f"{C_CYAN}▶ {metrics.symbol} | {metrics.exchange}{C_RESET}",
        "────────────────────────────────────────────────────────────",
        _format_pair(
            "現價",
            f"${metrics.current_price:.2f}",
            "IV Rank",
            f"{metrics.iv_rank:.1f}%",
            left_color=price_color,
            right_color=C_YELLOW if metrics.iv_rank > 65 else C_GREEN,
        ),
        _format_pair(
            "PE",
            f"{metrics.pe_ratio:.2f}" if metrics.pe_ratio is not None else "N/A",
            "RSI 14",
            f"{metrics.rsi_14:.2f}",
        ),
        _format_pair(
            "ATR 14",
            f"{metrics.atr_14:.2f}",
            "Beta",
            f"{metrics.beta:.2f}",
        ),
        _format_pair(
            "MA20/50",
            f"{metrics.ma20:.2f} / {metrics.ma50:.2f}",
            "MA200",
            f"{metrics.ma200:.2f}",
        ),
        _format_pair(
            "MA20 偏離",
            f"{metrics.bias_ma20 * 100:+.2f}%",
            "相對 SPY",
            f"{metrics.relative_strength_spy * 100:+.2f}%",
            left_color=bias_color,
            right_color=rs_color,
        ),
        "────────────────────────────────────────────────────────────",
        f"{C_CYAN}技術 / 防禦牆{C_RESET}",
        f"買點狀態      {metrics.buy_zone_status}",
        _format_pair(
            "Buy P1",
            f"${metrics.buy_price_phase1:.2f}",
            "Vol POC",
            f"${metrics.volume_poc:.2f}",
            left_color=C_GREEN,
        ),
        _format_pair(
            "Buy P2",
            f"${metrics.buy_price_phase2:.2f}",
            "GEX PutWall",
            f"${metrics.gex_max_put_wall:.2f}",
            left_color=C_YELLOW,
            right_color=C_YELLOW,
        ),
        _format_pair(
            "Buy P3",
            f"${metrics.buy_price_phase3:.2f}",
            "絕對支撐距",
            f"{metrics.distance_to_absolute_support * 100:+.2f}%",
            left_color=C_RED,
            right_color=support_color,
        ),
        _format_pair(
            "Sell P1",
            f"${metrics.sell_price_phase1:.2f}",
            "Sell P2",
            f"${metrics.sell_price_phase2:.2f}",
        ),
        _format_pair(
            "Sell P3",
            f"${metrics.sell_price_phase3:.2f}",
            "Vanna",
            f"{metrics.vanna_sensitivity:+.4f}",
        ),
        f"賣出狀態      {metrics.sell_zone_status}",
        "────────────────────────────────────────────────────────────",
        f"{C_CYAN}SDDM / 對沖{C_RESET}",
        _format_single("路由", tactical_model.sddm_route, color=route_color),
        _format_pair(
            "網格步長",
            f"{tactical_model.dynamic_grid_step:.2f}",
            "Hidden Δ",
            f"{tactical_model.hidden_delta_risk:+.2f}",
            left_color=C_YELLOW,
            right_color=hidden_delta_color,
        ),
        _format_single(
            "對沖股數",
            str(tactical_model.hedge_allocation_shares),
            color=C_RED if tactical_model.hedge_allocation_shares else C_GREEN,
        ),
    ]

    instruction_lines = _wrap_visual(
        tactical_model.action_guideline,
        width=55,
        indent=" " * 14,
    )
    lines.append(f"執行指南      {route_color}{instruction_lines[0]}{C_RESET}")
    for extra_line in instruction_lines[1:]:
        lines.append(f"{route_color}{extra_line}{C_RESET}")
    if tactical_model.hedge_instruction:
        hedge_lines = _wrap_visual(
            tactical_model.hedge_instruction,
            width=55,
            indent=" " * 14,
        )
        lines.append(f"對沖指令      {C_RED}{hedge_lines[0]}{C_RESET}")
        for extra_line in hedge_lines[1:]:
            lines.append(f"{C_RED}{extra_line}{C_RESET}")

    lines.append("```")
    return "\n".join(lines)
