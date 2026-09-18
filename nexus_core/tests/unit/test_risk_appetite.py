"""
tests/unit/test_risk_appetite.py

單元測試：階段 0 風險偏好參數化 (RiskAppetite / RiskProfile)。

涵蓋 handoff.md §2 的三個消費端：
  1. anti_washout.py 的 TP1 執行比例
  2. opportunity_cost.py 的機會成本轉倉 EV Spread 門檻基礎分量
  3. core_deployment.py 的核心資金機會分支部署比例

驗收標準 (§2.7)：未設定 risk_appetite (即 DEFENSIVE) 的行為必須與改動前逐位元
相同，故每個消費端皆有一個「預設值不變」測項，再搭配一個「AGGRESSIVE 確實
改變行為」測項。
"""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from market_analysis.dynamic_rollover import DynamicRolloverEngine
from market_analysis.dynamic_rollover.constants import (
    RiskProfile,
    _CORE_DEPLOYMENT_OPPORTUNITY_DEPLOY_RATIO,
    _EV_SPREAD_MIN_THRESHOLD,
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
    assert profile.ev_hurdle == _EV_SPREAD_MIN_THRESHOLD
    assert profile.core_deploy_ratio == _CORE_DEPLOYMENT_OPPORTUNITY_DEPLOY_RATIO


def test_aggressive_profile_values() -> None:
    profile = resolve_risk_profile("AGGRESSIVE")
    assert profile == RiskProfile(
        tp1_ratio=0.30,
        ev_hurdle=0.02,
        rotation_cooldown_days=3,
        core_deploy_ratio=0.80,
        max_satellite_budget_pct=0.25,
    )


# ============================================================================
# 消費端 1：anti_washout.py TP1 執行比例
# ============================================================================


def test_tp_ladder_default_tp1_ratio_unchanged(engine: DynamicRolloverEngine) -> None:
    metrics = {"spot_price": 100.0, "call_wall": 100.0, "quantity": 10.0}
    tier, ratio, _reason = engine._evaluate_microstructure_tp_ladder(metrics)
    assert tier == "TP1"
    assert ratio == _MICROSTRUCTURE_TP1_RATIO


def test_tp_ladder_aggressive_tp1_ratio_override(
    engine: DynamicRolloverEngine,
) -> None:
    metrics = {"spot_price": 100.0, "call_wall": 100.0, "quantity": 10.0}
    profile = resolve_risk_profile("AGGRESSIVE")
    tier, ratio, reason = engine._evaluate_microstructure_tp_ladder(
        metrics, tp1_ratio=profile.tp1_ratio
    )
    assert tier == "TP1"
    assert ratio == 0.30
    assert "30%" in reason


def test_tp_ladder_short_default_tp1_ratio_unchanged(
    engine: DynamicRolloverEngine,
) -> None:
    metrics = {"spot_price": 100.0, "put_wall": 100.0, "quantity": -10.0}
    tier, ratio, _reason = engine._evaluate_microstructure_tp_ladder(metrics)
    assert tier == "TP1"
    assert ratio == _MICROSTRUCTURE_TP1_RATIO


def test_tp_ladder_short_aggressive_tp1_ratio_override(
    engine: DynamicRolloverEngine,
) -> None:
    metrics = {"spot_price": 100.0, "put_wall": 100.0, "quantity": -10.0}
    tier, ratio, reason = engine._evaluate_microstructure_tp_ladder(
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


def test_evaluate_opportunity_cost_default_hurdle_unchanged(
    engine: DynamicRolloverEngine,
) -> None:
    # ev_spread = 0.25 - 0.20 = 0.05；預設門檻 0.05 + 0.003(摩擦成本) = 0.053 → 不觸發
    result = engine.evaluate_opportunity_cost(
        current_holding_symbol="NVDA",
        current_holding_power_squeeze=10.0,
        current_holding_profit_pct=0.1,
        target_watchlist_symbol="SMCI",
        target_power_squeeze=95.0,
        target_expected_value=0.25,
        current_holding_expected_value=0.20,
    )
    assert result["should_rollover"] is False


def test_evaluate_opportunity_cost_aggressive_hurdle_triggers_earlier(
    engine: DynamicRolloverEngine,
) -> None:
    # 同樣 ev_spread=0.05，AGGRESSIVE 基礎門檻 0.02 + 摩擦成本 0.003 = 0.023 → 觸發
    profile = resolve_risk_profile("AGGRESSIVE")
    result = engine.evaluate_opportunity_cost(
        current_holding_symbol="NVDA",
        current_holding_power_squeeze=10.0,
        current_holding_profit_pct=0.1,
        target_watchlist_symbol="SMCI",
        target_power_squeeze=95.0,
        target_expected_value=0.25,
        current_holding_expected_value=0.20,
        base_ev_hurdle_pct=profile.ev_hurdle,
    )
    assert result["should_rollover"] is True


@pytest.mark.asyncio
@patch(
    "market_analysis.dynamic_rollover.DynamicRolloverEngine._confirm_entry_signal",
    new_callable=AsyncMock,
    return_value=(True, "mocked", None),
)
@patch("database.market_cache.get_market_cache")
@patch("market_analysis.dynamic_rollover.opportunity_cost.get_full_user_context")
async def test_evaluate_opportunity_cost_for_satellites_threads_risk_profile_ev_hurdle(
    mock_get_user: MagicMock,
    mock_cache: MagicMock,
    mock_entry_gate: AsyncMock,
    engine: DynamicRolloverEngine,
) -> None:
    """evaluate_opportunity_cost_for_satellites 必須把解析出的
    RiskProfile.ev_hurdle 傳給 evaluate_opportunity_cost 作為
    base_ev_hurdle_pct，且與既有的動態摩擦成本 (friction_cost_pct) 並存，
    而非互相覆蓋 (見 constants.py::RiskProfile.ev_hurdle 的 docstring)。"""
    mock_get_user.return_value = MagicMock(
        trading_strategy="RIGHT_SIDE", risk_appetite="AGGRESSIVE"
    )

    def cache_side_effect(symbol: str, expiry: str = None):  # type: ignore
        if symbol.upper() == "NVDA":
            return {
                "reference_spot_price": 200.0,
                "expected_move_upper": 205.0,
                "is_stale": 0,
                "is_degraded": 0,
            }
        if symbol.upper() == "SMCI":
            return {
                "reference_spot_price": 40.0,
                "expected_move_upper": 50.0,
                "is_stale": 0,
                "is_degraded": 0,
            }
        return None

    mock_cache.side_effect = cache_side_effect

    portfolio = [
        {
            "symbol": "NVDA",
            "asset_class": "SATELLITE",
            "spot_price": 240.0,
            "avg_cost": 200.0,
            "psq_result": {
                "squeeze_level": "Release",
                "signal_direction": "Neutral",
            },
        },
    ]
    candidate_radar = {
        "psq_result": {
            "squeeze_level": "High",
            "signal_direction": "Long",
            "is_breakout_long": True,
        },
        "quote": {"c": 40.0},
        "iv_metrics": {"iv_rank": 20.0},
        "gex_profile_data": {"put_wall": 0.0},
        "uoa": [],
    }

    spy = MagicMock(wraps=engine.evaluate_opportunity_cost)
    with patch.object(engine, "evaluate_opportunity_cost", spy):
        await engine.evaluate_opportunity_cost_for_satellites(
            1, portfolio, set(), "SMCI", candidate_radar
        )

    spy.assert_called()
    assert spy.call_args is not None
    assert spy.call_args.kwargs["base_ev_hurdle_pct"] == pytest.approx(0.02)


# ============================================================================
# 消費端 3：core_deployment.py 核心資金機會分支部署比例
# ============================================================================


@pytest.mark.asyncio
@patch(
    "market_analysis.dynamic_rollover.DynamicRolloverEngine._confirm_entry_signal",
    new_callable=AsyncMock,
    return_value=(True, "mocked", None),
)
async def test_evaluate_core_deployment_default_ratio_unchanged(
    mock_entry_gate: AsyncMock,
    engine: DynamicRolloverEngine,
) -> None:
    portfolio = [
        {
            "symbol": "VOO",
            "asset_class": "CORE",
            "current_value": 10000.0,
            "target_allocation_pct": 0.5,
            "boxx_allocation_pct": 0.0,
        },
    ]
    candidate_radar = {
        "psq_result": {
            "squeeze_level": "High",
            "signal_direction": "Long",
            "is_breakout_long": True,
        },
        "quote": {"c": 40.0},
        "iv_metrics": {"iv_rank": 20.0},
        "gex_profile_data": {"put_wall": 0.0},
        "uoa": [],
    }
    # 超額 = 5000；DEFENSIVE 部署 50% (現行行為) = 2500 -> sell_ratio = 0.25
    result = await engine.evaluate_core_deployment(
        1, portfolio, set(), 10000.0, "SPCX", candidate_radar
    )
    assert len(result) == 1
    assert result[0]["sell_ratio"] == 0.25


@pytest.mark.asyncio
@patch(
    "market_analysis.dynamic_rollover.DynamicRolloverEngine._confirm_entry_signal",
    new_callable=AsyncMock,
    return_value=(True, "mocked", None),
)
@patch("market_analysis.dynamic_rollover.core_deployment.get_full_user_context")
async def test_evaluate_core_deployment_aggressive_deploys_more(
    mock_get_user: MagicMock,
    mock_entry_gate: AsyncMock,
    engine: DynamicRolloverEngine,
) -> None:
    mock_get_user.return_value = MagicMock(risk_appetite="AGGRESSIVE")
    portfolio = [
        {
            "symbol": "VOO",
            "asset_class": "CORE",
            "current_value": 10000.0,
            "target_allocation_pct": 0.5,
            "boxx_allocation_pct": 0.0,
        },
    ]
    candidate_radar = {
        "psq_result": {
            "squeeze_level": "High",
            "signal_direction": "Long",
            "is_breakout_long": True,
        },
        "quote": {"c": 40.0},
        "iv_metrics": {"iv_rank": 20.0},
        "gex_profile_data": {"put_wall": 0.0},
        "uoa": [],
    }
    # 超額 = 5000；AGGRESSIVE 部署 80% = 4000 -> sell_ratio = 0.4
    result = await engine.evaluate_core_deployment(
        1, portfolio, set(), 10000.0, "SPCX", candidate_radar
    )
    assert len(result) == 1
    assert result[0]["sell_ratio"] == 0.4


@pytest.mark.asyncio
@patch("market_analysis.dynamic_rollover.core_deployment.get_full_user_context")
async def test_evaluate_core_deployment_unknown_risk_appetite_falls_back_defensive(
    mock_get_user: MagicMock,
    engine: DynamicRolloverEngine,
) -> None:
    """讀取使用者設定失敗時必須 fail-safe 回退 DEFENSIVE，不得讓例外向上傳播
    中斷整個部署評估。沿用既有 test_evaluate_core_deployment_boxx_defense_
    manual_threshold 的 BOXX 防禦分支數值組合 (該分支本就不受風險偏好影響，
    僅用於驗證讀取失敗不會拋例外)。"""
    mock_get_user.side_effect = Exception("mocked db failure")
    portfolio = [
        {
            "symbol": "VOO",
            "asset_class": "CORE",
            "current_value": 10000.0,
            "target_allocation_pct": 0.5,
            "boxx_allocation_pct": 0.7,  # 70 >= _BOXX_DEFENSE_THRESHOLD (50)
        },
    ]
    result = await engine.evaluate_core_deployment(
        1, portfolio, set(), 10000.0, "SPCX", None
    )
    assert len(result) == 1
    assert result[0]["target_core"] == "BOXX"
    assert result[0]["sell_ratio"] == 0.5
