"""IVR 策略硬鎖閘門的單元測試。"""

from market_analysis.ivr_strategy_gate import (
    is_selling_locked_by_ivr,
    _IVR_SELLING_LOCKOUT,
    get_ivr_lockout_allowed_strategies,
)


def test_ivr_below_lockout_locks_selling() -> None:
    """IVR < 10% → 鎖死賣方策略"""
    assert is_selling_locked_by_ivr(5.0) is True
    assert is_selling_locked_by_ivr(9.9) is True


def test_ivr_at_lockout_does_not_lock() -> None:
    """IVR == 10% → 不鎖死（精確邊界）"""
    assert is_selling_locked_by_ivr(10.0) is False


def test_ivr_above_lockout_does_not_lock() -> None:
    """IVR > 10% → 正常路由，不鎖死"""
    assert is_selling_locked_by_ivr(15.0) is False
    assert is_selling_locked_by_ivr(50.0) is False
    assert is_selling_locked_by_ivr(100.0) is False


def test_ivr_zero_does_not_lock() -> None:
    """IVR == 0.0（數據缺失）→ 不觸發鎖死，由其他降級邏輯處理"""
    assert is_selling_locked_by_ivr(0.0) is False


def test_ivr_negative_does_not_lock() -> None:
    """IVR 負值（異常值）→ 不觸發鎖死"""
    assert is_selling_locked_by_ivr(-1.0) is False


def test_lockout_constant_is_10() -> None:
    """確認硬鎖門檻常數為 10.0"""
    assert _IVR_SELLING_LOCKOUT == 10.0


def test_allowed_strategies_in_lockout() -> None:
    """鎖死狀態下的允許策略列表應包含現貨、ITM Call BTO、Debit Spread"""
    allowed = get_ivr_lockout_allowed_strategies()
    assert "SPOT_BUY" in allowed
    assert "BTO_CALL_ITM" in allowed
    assert "DEBIT_SPREAD" in allowed
    # 不應包含任何賣方策略
    for strategy in allowed:
        assert "STO" not in strategy
        assert "SELL" not in strategy
