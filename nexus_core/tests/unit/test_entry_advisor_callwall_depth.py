"""進場顧問目標價：薄遠端 CallWall 不得當目標（比照 /x 深度規則，僅顯示用）。"""

from typing import Any
from unittest.mock import AsyncMock, patch

import pytest

from market_analysis.intraday_pipeline import entry_advisor

pytestmark = pytest.mark.asyncio

_DEEP = 303_526_351.95
_THIN = 2_041_178.28


async def _levels(
    gex: dict[str, Any], spot: float, high_60d: float
) -> tuple[Any, Any, Any, Any]:
    radar = {"quote": {"c": spot}, "gex_profile_data": gex}
    with (
        patch(
            "market_analysis.dynamic_rollover._shared.resolve_room_threshold_inputs",
            AsyncMock(return_value=(spot - 4.0, 1.0, 3.0)),
        ),
        patch(
            "market_analysis.dynamic_rollover.structural_signals._scan_gex_walls",
            return_value=(spot - 4.0, 0.0, 1.0, 0.0),
        ),
        patch(
            "market_analysis.atr_utils.fetch_high_60d",
            AsyncMock(return_value=high_60d),
        ),
    ):
        return await entry_advisor._compute_long_levels(
            "VOO", radar, spot, None, None, False
        )


async def test_thin_edge_wall_reanchored_to_deep_wall() -> None:
    gex = {
        "call_wall": 790.0,
        "gex_profile": {"710.0": _DEEP, "790.0": _THIN},
    }
    _e, _s, target, rr = await _levels(gex, 700.0, 800.0)
    assert target == 710.0
    assert rr == round((710.0 - 700.0) / (700.0 - _s), 2)


async def test_thin_edge_wall_no_reanchor_gives_none() -> None:
    gex = {"call_wall": 790.0, "gex_profile": {"690.0": _DEEP, "790.0": _THIN}}
    _e, _s, target, rr = await _levels(gex, 700.0, 800.0)
    assert target is None and rr is None


async def test_thin_edge_wall_near_high_uses_fallback_ladder() -> None:
    from market_analysis.room_threshold import resolve_effective_target

    gex = {"call_wall": 790.0, "gex_profile": {"690.0": _DEEP, "790.0": _THIN}}
    _e, _s, target, _rr = await _levels(gex, 790.0 - 5.0, 790.0)
    expected = resolve_effective_target(785.0, 0.0, 790.0, 3.0).target
    assert target == round(expected, 2)
    assert target != 790.0
    assert target == 794.0  # ATR 外推 785 + 3×3


async def test_thick_edge_wall_unchanged() -> None:
    gex = {
        "call_wall": 720.0,
        "gex_profile": {"720.0": _DEEP, "710.0": _DEEP / 2},
    }
    _e, _s, target, _rr = await _levels(gex, 700.0, 800.0)
    assert target == 720.0


async def test_edge_wall_missing_from_profile_kept() -> None:
    gex = {"call_wall": 750.0, "gex_profile": {"710.0": _DEEP}}
    _e, _s, target, _rr = await _levels(gex, 700.0, 800.0)
    assert target == 750.0
