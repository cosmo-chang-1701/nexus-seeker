"""執行管線 Stage 1 的交易意圖分流 (VIX 戰情階梯方向感知化)。"""

from typing import Any

from services.trading_service.execution import ExecutionMixin


def _data(strategy: str, vix: float, allow: bool) -> dict[str, Any]:
    return {
        "strategy": strategy,
        "vix_spot": vix,
        "vix_tier_name": "tier",
        "vix_allow_signal": allow,
        "aroc": 50.0,
        "safe_qty": 1,
    }


def test_stage1_rejects_directional_short_in_vix_extreme() -> None:
    ok, reason = ExecutionMixin()._validate_trade_pipeline(
        None, _data("BTO_PUT", 38.0, True)
    )
    assert ok is False
    assert "做空新倉暫停" in reason


def test_stage1_dormant_rejects_seller_but_not_short() -> None:
    mixin = ExecutionMixin()
    ok_sto, reason_sto = mixin._validate_trade_pipeline(
        None, _data("STO_PUT", 12.0, False)
    )
    assert ok_sto is False and "restricts STO" in reason_sto
    ok_put, _ = mixin._validate_trade_pipeline(None, _data("BTO_PUT", 12.0, False))
    assert ok_put is True
