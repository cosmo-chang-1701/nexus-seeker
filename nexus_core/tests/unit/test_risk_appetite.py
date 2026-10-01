"""
tests/unit/test_risk_appetite.py

單元測試：階段 0 風險偏好參數化 (RiskAppetite / RiskProfile)。

涵蓋 handoff.md §2 的消費端（核心資金部署分支已移除）：
  1. anti_washout.py 的 TP1 執行比例
  2. opportunity_cost.py 的機會成本轉倉 EV Spread 門檻基礎分量

驗收標準 (§2.7)：未設定 risk_appetite (即 DEFENSIVE) 的行為必須與改動前逐位元
相同，故每個消費端皆有一個「預設值不變」測項，再搭配一個「AGGRESSIVE 確實
改變行為」測項。
"""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from market_analysis.dynamic_rollover import DynamicRolloverEngine
from market_analysis.dynamic_rollover.constants import (
    RiskProfile,
    _MICROSTRUCTURE_TP1_RATIO,
    resolve_risk_profile,
)


@pytest.fixture
def engine() -> DynamicRolloverEngine:
    return DynamicRolloverEngine()


# ============================================================================
# resolve_risk_profile() 查表本身
# ============================================================================


def test_resolve_risk_profile_unknown_value_falls_back_to_defensive() -> None:
    assert resolve_risk_profile("NOT_A_REAL_PROFILE") == resolve_risk_profile(
        "DEFENSIVE"
    )


def test_resolve_risk_profile_none_falls_back_to_defensive() -> None:
    assert resolve_risk_profile(None) == resolve_risk_profile("DEFENSIVE")


def test_resolve_risk_profile_empty_string_falls_back_to_defensive() -> None:
    assert resolve_risk_profile("") == resolve_risk_profile("DEFENSIVE")


def test_resolve_risk_profile_case_insensitive() -> None:
    assert resolve_risk_profile("aggressive") == resolve_risk_profile("AGGRESSIVE")


def test_defensive_profile_matches_existing_constants_bit_identical() -> None:
    """DEFENSIVE 組必須與改動前散落各處的個別常數完全相同，否則未設定
    risk_appetite 的既有使用者行為會被靜默改變 (§2.7 驗收標準)。"""
    profile = resolve_risk_profile("DEFENSIVE")
    assert profile.tp1_ratio == _MICROSTRUCTURE_TP1_RATIO


def test_aggressive_profile_values() -> None:
    profile = resolve_risk_profile("AGGRESSIVE")
    assert profile == RiskProfile(
        tp1_ratio=0.30,
        rotation_cooldown_days=3,
        max_satellite_budget_pct=0.25,
    )


# ============================================================================
# 消費端 1：anti_washout.py TP1 執行比例
# ============================================================================


def test_tp_ladder_default_tp1_ratio_unchanged(engine: DynamicRolloverEngine) -> None:
    metrics = {"spot_price": 100.0, "call_wall": 100.0, "quantity": 10.0}
    tier, ratio, _reason, _new_stop = engine._evaluate_microstructure_tp_ladder(metrics)
    assert tier == "TP1"
    assert ratio == _MICROSTRUCTURE_TP1_RATIO


def test_tp_ladder_aggressive_tp1_ratio_override(
    engine: DynamicRolloverEngine,
) -> None:
    metrics = {"spot_price": 100.0, "call_wall": 100.0, "quantity": 10.0}
    profile = resolve_risk_profile("AGGRESSIVE")
    tier, ratio, reason, _new_stop = engine._evaluate_microstructure_tp_ladder(
        metrics, tp1_ratio=profile.tp1_ratio
    )
    assert tier == "TP1"
    assert ratio == 0.30
    assert "30%" in reason


def test_tp_ladder_short_default_tp1_ratio_unchanged(
    engine: DynamicRolloverEngine,
) -> None:
    metrics = {"spot_price": 100.0, "put_wall": 100.0, "quantity": -10.0}
    tier, ratio, _reason, _new_stop = engine._evaluate_microstructure_tp_ladder(metrics)
    assert tier == "TP1"
    assert ratio == _MICROSTRUCTURE_TP1_RATIO


def test_tp_ladder_short_aggressive_tp1_ratio_override(
    engine: DynamicRolloverEngine,
) -> None:
    metrics = {"spot_price": 100.0, "put_wall": 100.0, "quantity": -10.0}
    tier, ratio, reason, _new_stop = engine._evaluate_microstructure_tp_ladder(
        metrics, tp1_ratio=0.30
    )
    assert tier == "TP1"
    assert ratio == 0.30
    assert "30%" in reason


@pytest.mark.asyncio
@patch("market_analysis.dynamic_rollover.get_full_user_context")
async def test_check_satellite_rebalancing_threads_risk_profile_tp1_ratio(
    mock_get_user: MagicMock, engine: DynamicRolloverEngine
) -> None:
    """check_satellite_rebalancing_impl 必須在入口解析一次 RiskProfile，並把
    tp1_ratio 一路傳進 _generate_rule_based_rebalance_report，而非停留在
    _evaluate_microstructure_tp_ladder 的單元層級。"""
    mock_get_user.return_value = MagicMock(
        risk_appetite="AGGRESSIVE", can_trade_spreads=False
    )
    portfolio = [
        {
            "symbol": "NVDA",
            "asset_class": "SATELLITE",
            "current_value": 4500.0,
            "target_allocation_pct": 0.20,
            "max_allocation_pct": 0.30,
        },
        {
            "symbol": "VOO",
            "asset_class": "CORE",
            "current_value": 5500.0,
            "target_allocation_pct": 0.80,
            "max_allocation_pct": 1.0,
        },
    ]
    spy = AsyncMock(wraps=engine._generate_rule_based_rebalance_report)
    with patch.object(engine, "_generate_rule_based_rebalance_report", spy):
        await engine.check_satellite_rebalancing(1, portfolio, 10000.0)

    spy.assert_awaited()
    assert spy.await_args is not None
    assert spy.await_args.kwargs["tp1_ratio"] == 0.30


# ============================================================================
# 消費端 2：opportunity_cost.py 機會成本轉倉 EV Spread 門檻基礎分量
# ============================================================================
