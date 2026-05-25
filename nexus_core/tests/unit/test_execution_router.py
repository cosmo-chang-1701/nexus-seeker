import pytest
from models.execution import MarketCondition
from services.execution_router import ExecutionRouter


@pytest.fixture
def router():
    """
    提供 ExecutionRouter 實例的 Pytest Fixture。
    """
    return ExecutionRouter()


def test_gatekeeper_shield_on_high_vix(router):
    """
    當 VIX > 25 時，應路由至 SHIELD (Module A)。
    """
    condition = MarketCondition(
        vix=26.0,
        skew_percent=0.02,  # 2%
        asset_price=100.0,
        ma20=100.0,
        atr_14=2.0,
        rsi_14=50.0,
        uoa_detected=False,
    )
    decision = router.evaluate_market(condition)
    assert decision.decision_type == "SHIELD"
    assert "VIX" in decision.trigger_reason


def test_gatekeeper_shield_on_high_skew(router):
    """
    當 Skew 絕對值 > 5% 時，應路由至 SHIELD (Module A)。
    """
    condition = MarketCondition(
        vix=15.0,
        skew_percent=-0.051,  # -5.1%
        asset_price=100.0,
        ma20=100.0,
        atr_14=2.0,
        rsi_14=50.0,
        uoa_detected=False,
    )
    decision = router.evaluate_market(condition)
    assert decision.decision_type == "SHIELD"
    assert "偏度" in decision.trigger_reason


def test_gatekeeper_shield_on_price_deviation(router):
    """
    當價格偏離 MA20 超過 10% 時，應路由至 SHIELD (Module A)。
    """
    condition = MarketCondition(
        vix=15.0,
        skew_percent=0.02,
        asset_price=111.0,  # +11% 乖離
        ma20=100.0,
        atr_14=2.0,
        rsi_14=50.0,
        uoa_detected=False,
    )
    decision = router.evaluate_market(condition)
    assert decision.decision_type == "SHIELD"
    assert "乖離" in decision.trigger_reason


def test_gatekeeper_spear_on_uoa(router):
    """
    當市場環境正常且偵測到 UOA 時，應路由至 SPEAR (Module B)。
    """
    condition = MarketCondition(
        vix=15.0,
        skew_percent=0.02,
        asset_price=101.0,
        ma20=100.0,
        atr_14=2.0,
        rsi_14=50.0,
        uoa_detected=True,
    )
    decision = router.evaluate_market(condition)
    assert decision.decision_type == "SPEAR"
    assert "UOA" in decision.trigger_reason


def test_gatekeeper_standby_on_neutral(router):
    """
    當無明顯信號時，應處於 STANDBY 狀態。
    """
    condition = MarketCondition(
        vix=15.0,
        skew_percent=0.02,
        asset_price=101.0,
        ma20=100.0,
        atr_14=2.0,
        rsi_14=50.0,
        uoa_detected=False,
    )
    decision = router.evaluate_market(condition)
    assert decision.decision_type == "STANDBY"


def test_atr_grid_widening_logic(router):
    """
    驗證 ATR 網格寬度計算邏輯。
    """
    condition = MarketCondition(
        vix=30.0,
        skew_percent=0.0,
        asset_price=100.0,
        ma20=100.0,
        atr_14=1.0,  # (1.0 * 1.2) / 100 = 0.012 (1.2%)
        rsi_14=50.0,
        uoa_detected=False,
    )
    decision = router.evaluate_market(condition)
    assert decision.grid_params.dynamic_step_percent > 0

    # 測試更高 ATR 導致更寬網格
    condition_high_atr = condition.model_copy(
        update={"atr_14": 2.0}
    )  # (2.0 * 1.2) / 100 = 0.024 (2.4%)
    decision_high = router.evaluate_market(condition_high_atr)
    assert (
        decision_high.grid_params.dynamic_step_percent
        > decision.grid_params.dynamic_step_percent
    )


def test_kelly_sizing_cap(router):
    """
    驗證凱利公式倉位限額邏輯。
    """
    condition = MarketCondition(
        vix=15.0,
        skew_percent=0.0,
        asset_price=100.0,
        ma20=100.0,
        atr_14=2.0,
        rsi_14=30.0,
        uoa_detected=True,
    )
    decision = router.evaluate_market(condition)
    assert decision.decision_type == "SPEAR"
    # 凱利百分比不應超過安全上限 (例如 0.15)
    assert decision.position_sizing.kelly_percentage <= 0.15


def test_gatekeeper_spear_on_overextended_bullish_and_high_rs(router):
    """
    當價格嚴重超買 (偏離 > 10% AND RSI > 65) 且相對強度 RS > 1.2 時，應戰術路由至 SPEAR 模式。
    """
    # Case 1: Overextended bullish and RS > 1.2 -> SPEAR
    condition_spear = MarketCondition(
        vix=15.0,
        skew_percent=0.02,
        asset_price=112.0,  # 12% deviation from ma20
        ma20=100.0,
        atr_14=2.0,
        rsi_14=70.0,  # > 65
        uoa_detected=False,
        relative_strength=1.3,  # > 1.2
    )
    decision = router.evaluate_market(condition_spear)
    assert decision.decision_type == "SPEAR"
    assert "相對行業板塊強度 RS" in decision.trigger_reason

    # Case 2: Overextended bullish but RS <= 1.2 -> SHIELD (falling back to standard deviation grid)
    condition_shield = MarketCondition(
        vix=15.0,
        skew_percent=0.02,
        asset_price=112.0,
        ma20=100.0,
        atr_14=2.0,
        rsi_14=70.0,
        uoa_detected=False,
        relative_strength=1.1,  # <= 1.2
    )
    decision_shield = router.evaluate_market(condition_shield)
    assert decision_shield.decision_type == "SHIELD"
    assert "乖離" in decision_shield.trigger_reason
