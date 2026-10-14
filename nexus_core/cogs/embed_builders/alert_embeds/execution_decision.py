"""NRO 執行決策矩陣 Embed（ExecutionDecision → NexusEmbed）。

原位於 `formatters/execution_embeds.py`，直接建構 dict／discord.Embed 而繞過
NexusEmbed 契約（色票、footer、時間戳記）；現統一收斂至 embed_builders。
"""

from __future__ import annotations

import discord

from cogs.embed_builders._core import NexusEmbed
from cogs.embed_builders._embed_helpers import _add_ansi_field_safely
from models.execution import ExecutionDecision

C_RESET = "\u001b[0m"
C_CYAN = "\u001b[1;36m"

_ROUTING_LABELS = {
    "SHIELD": "Module A (The Shield)",
    "SPEAR": "Module B (The Spear)",
}


def _decision_color(decision_type: str) -> discord.Color:
    if decision_type == "SHIELD":
        return discord.Color.red()
    if decision_type == "SPEAR":
        return discord.Color.green()
    return discord.Color.default()


def create_execution_decision_embed(decision: ExecutionDecision) -> NexusEmbed:
    """將 ExecutionDecision 轉為 NexusEmbed（供 NRO 掃描私訊推播）。

    - SHIELD：紅（防禦網）；SPEAR：綠（精準打擊）；STANDBY／未知：中性。
    - 觸發原因獨立為欄位；各參數欄位以 ansi 樹狀呈現，全部 inline=False。
    """
    embed = NexusEmbed(
        title=f"🛡️ 執行決策：{decision.decision_type}",
        color=_decision_color(decision.decision_type),
    )

    embed.add_field(name="⚡ 觸發原因", value=decision.trigger_reason, inline=False)

    routing = _ROUTING_LABELS.get(decision.decision_type, "Idle (Standby)")
    _add_ansi_field_safely(
        embed,
        "📊 Macro Status",
        [
            "```ansi",
            f" ├─ 決策類型：{C_CYAN}{decision.decision_type}{C_RESET}",
            f" └─ 路由模組：{C_CYAN}{routing}{C_RESET}",
            "```",
        ],
    )

    if decision.decision_type == "SHIELD" and decision.grid_params:
        params = decision.grid_params
        _add_ansi_field_safely(
            embed,
            "🕸️ Grid Spacing",
            [
                "```ansi",
                f" ├─ 基準價格：{C_CYAN}${params.base_price:,.2f}{C_RESET}",
                f" └─ 動態步長：{C_CYAN}{params.dynamic_step_percent * 100:.2f}%{C_RESET}",
                "```",
            ],
        )
    elif decision.decision_type == "SPEAR" and decision.position_sizing:
        sizing = decision.position_sizing
        _add_ansi_field_safely(
            embed,
            "⚖️ Position Sizing / Risk Caps",
            [
                "```ansi",
                f" ├─ 凱利建議：{C_CYAN}{sizing.kelly_percentage * 100:.2f}%{C_RESET}",
                f" ├─ 最大分配：{C_CYAN}${sizing.max_capital_allocation:,.0f}{C_RESET}",
                f" └─ Theta 限制：{C_CYAN}-${sizing.max_theta_exposure:,.2f}/日{C_RESET}",
                "```",
            ],
        )

    if decision.exit_strategy:
        exit_strat = decision.exit_strategy
        trailing = "已啟用" if exit_strat.trailing_stop_active else "未啟用"
        _add_ansi_field_safely(
            embed,
            "🚪 Exit Strategy",
            [
                "```ansi",
                f" ├─ 移動止損：{C_CYAN}{trailing}{C_RESET}",
                f" ├─ 觸發價格：{C_CYAN}${exit_strat.trigger_price:,.2f}{C_RESET}",
                f" └─ 條件類型：{C_CYAN}{exit_strat.condition_type}{C_RESET}",
                "```",
            ],
        )

    embed.set_footer(text="執行決策矩陣 v1.0")
    return embed
