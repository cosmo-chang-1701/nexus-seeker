"""執行管線 Stage 1 與 VTR 建倉閘門的交易意圖分流 (VIX 戰情階梯方向感知化)。"""

from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from services.trading_service.execution import ExecutionMixin
from services.trading_service.vtr import VtrMixin


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


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("strategy", "vix", "expected_calls"),
    [
        ("BTO_PUT", 12.0, 1),  # 休兵區：賣方禁建倉，但做空 0.5x 仍允許
        ("STO_PUT", 12.0, 0),
        ("BTO_PUT", 38.0, 0),  # 極端區：做空係數 0
        ("STO_PUT", 38.0, 1),
    ],
)
async def test_vtr_entry_gate_is_intent_aware(
    strategy: str, vix: float, expected_calls: int
) -> None:
    mixin = VtrMixin()
    mixin.vtr_engine = MagicMock()
    mixin.vtr_engine.record_virtual_entry = AsyncMock()
    await mixin.execute_vtr_auto_entry(
        {
            "uid": 1,
            "symbol": "AAPL",
            "strategy": strategy,
            "safe_qty": 1,
            "vix_spot": vix,
            "strike": 100.0,
            "target_date": "2026-12-18",
        }
    )
    assert mixin.vtr_engine.record_virtual_entry.await_count == expected_calls
