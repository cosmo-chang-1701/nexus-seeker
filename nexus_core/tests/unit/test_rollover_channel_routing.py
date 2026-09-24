"""動態轉倉指令 → 通知頻道對照（database/notification_channels.resolve_rollover_channel）。"""

import pytest

from database.notification_channels import (
    ALL_NOTIFICATION_KEYS,
    CHANNELS_BY_KEY,
    ROLLOVER_SCENARIO_CHANNEL,
    STRUCTURE_BREAK_EXIT_TIERS,
    resolve_rollover_channel,
)
from market_analysis.dynamic_rollover.models import RolloverScenario


def test_every_rollover_scenario_has_a_channel() -> None:
    """窮舉：新增 RolloverScenario 時必須決定它屬於哪個頻道。"""
    missing = [
        s.value for s in RolloverScenario if s.value not in ROLLOVER_SCENARIO_CHANNEL
    ]
    assert not missing, f"未對照通知頻道的轉倉情境: {missing}"
    assert set(ROLLOVER_SCENARIO_CHANNEL.values()) <= set(ALL_NOTIFICATION_KEYS)


@pytest.mark.parametrize(
    ("ins", "expected"),
    [
        ({"scenario": "MARGIN_DEFENSE", "action": "LIQUIDATE"}, "defense_margin_call"),
        # 保證金防禦即使帶結構失效分層也不改道
        (
            {"scenario": "MARGIN_DEFENSE", "exit_tier": "SL_STRUCTURAL"},
            "defense_margin_call",
        ),
        (
            {"scenario": "SATELLITE_REBALANCE", "exit_tier": "SL_STRUCTURAL"},
            "defense_structure_break",
        ),
        (
            {"scenario": "SATELLITE_REBALANCE", "exit_tier": "SL_REGIME_FLIP"},
            "defense_structure_break",
        ),
        (
            {"scenario": "SATELLITE_REBALANCE", "exit_tier": "SL_WHALE_PUT"},
            "defense_structure_break",
        ),
        (
            {"scenario": "SATELLITE_REBALANCE", "exit_tier": "EXTREME_TICK_BREACH"},
            "defense_structure_break",
        ),
        (
            {"scenario": "SATELLITE_REBALANCE", "exit_tier": "IVR_FAST_EXIT"},
            "defense_structure_break",
        ),
        # 動態保本是獲利部位管理，不是結構失效
        (
            {"scenario": "SATELLITE_REBALANCE", "exit_tier": "SL_TRAILING_BREAKEVEN"},
            "defense_option_rollover",
        ),
        (
            {"scenario": "SATELLITE_REBALANCE", "exit_tier": "TP1"},
            "defense_option_rollover",
        ),
        (
            {"scenario": "SATELLITE_REBALANCE", "exit_tier": "TP1_TREND_EXEMPT"},
            "defense_option_rollover",
        ),
        (
            {"scenario": "SATELLITE_REBALANCE", "exit_tier": None},
            "defense_option_rollover",
        ),
        ({"scenario": "OPPORTUNITY_COST"}, "defense_option_rollover"),
        # 顧問模式：結構失效告知走左尾防護，目標區走位階顧問
        (
            {
                "scenario": "SATELLITE_REBALANCE",
                "action": "ADVISORY",
                "exit_tier": "SL_STRUCTURAL",
            },
            "defense_structure_break",
        ),
        (
            {
                "scenario": "SATELLITE_REBALANCE",
                "action": "ADVISORY",
                "exit_tier": "TP2",
            },
            "advisory_core_levels",
        ),
        ({"scenario": "MACRO_TOP_ESCAPE_DEFENSE"}, "defense_structure_break"),
        ({"scenario": "CORE_DEPLOYMENT"}, "entry_pyramid_add"),
        (
            {"scenario": "CORE_DEPLOYMENT", "is_covered_call_overlay": True},
            "trim_covered_call",
        ),
        ({"scenario": "COVERED_CALL_PROFIT_LOCK"}, "trim_covered_call"),
        (
            {"scenario": "TRANSITION_ENGINE", "exit_tier": "TRANSITION_RATCHET"},
            "entry_pyramid_add",
        ),
        ({"scenario": "PYRAMID_ADD"}, "entry_pyramid_add"),
        ({"scenario": "SHORT_ENTRY"}, "alpha_short_entry"),
        ({"scenario": "UNKNOWN"}, "defense_option_rollover"),
    ],
)
def test_resolve_rollover_channel(ins: dict, expected: str) -> None:
    assert resolve_rollover_channel(ins) == expected


def test_structure_break_channel_is_left_tail_and_immune() -> None:
    c = CHANNELS_BY_KEY["defense_structure_break"]
    assert c.risk_role == "LEFT_TAIL"
    assert c.preset_immune
    assert "SL_TRAILING_BREAKEVEN" not in STRUCTURE_BREAK_EXIT_TIERS
