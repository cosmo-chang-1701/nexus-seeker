"""get_market_regime_inputs() 輸入快照（−3x 啟動紅線資料來源）單元測試。"""

import time
from typing import Any, Iterator
from unittest.mock import AsyncMock, patch

import pytest

import market_analysis.index_microstructure as im
from services.single_flight import SingleFlightManager


@pytest.fixture(autouse=True)
def _reset() -> Iterator[None]:
    def _do() -> None:
        im._market_regime_cache_value = None
        im._market_regime_cache_expiry = 0.0
        im._market_regime_inputs_value = None
        SingleFlightManager._active_tasks.clear()

    _do()
    yield
    _do()


def _patches(vix: float, vts: float, spot: float, flip: float) -> Any:
    from contextlib import ExitStack

    stack = ExitStack()
    stack.enter_context(
        patch(
            "services.market_data_service.get_vix_spot_strict",
            new=AsyncMock(return_value=vix),
        )
    )
    stack.enter_context(
        patch(
            "services.market_data_service.get_vix_term_structure",
            new=AsyncMock(return_value={"vts_ratio": vts, "is_valid": True}),
        )
    )
    stack.enter_context(
        patch(
            "services.market_data_service.get_quote",
            new=AsyncMock(return_value={"c": spot}),
        )
    )
    stack.enter_context(
        patch.object(
            im,
            "fetch_gex_metrics",
            new=AsyncMock(return_value={"spy_spot": spot, "gamma_flip": flip}),
        )
    )
    stack.enter_context(
        patch.object(
            im,
            "fetch_liquidity_metrics",
            new=AsyncMock(return_value={"_is_fallback": True}),
        )
    )
    return stack


async def test_redline_inputs_snapshot_normal() -> None:
    with _patches(15.0, 0.9, 520.0, 515.0):
        inputs = await im.get_market_regime_inputs()
    assert inputs is not None
    assert inputs.regime == "NORMAL"
    assert inputs.vix == 15.0
    assert inputs.vts_ratio == 0.9
    assert inputs.spy_spot == 520.0
    assert inputs.gamma_flip == 515.0


async def test_redline_inputs_snapshot_short_gamma_critical() -> None:
    with _patches(25.0, 1.05, 505.0, 515.0):
        inputs = await im.get_market_regime_inputs()
    assert inputs is not None
    assert inputs.regime == "SHORT_GAMMA_CRITICAL"
    assert inputs.vix == 25.0
    assert inputs.gamma_flip == 515.0


async def test_redline_inputs_cleared_by_invalidate() -> None:
    with _patches(25.0, 1.05, 505.0, 515.0):
        await im.get_market_regime_inputs()
    assert im._market_regime_inputs_value is not None
    im.invalidate_market_regime_cache()
    assert im._market_regime_inputs_value is None


async def test_redline_inputs_none_when_regime_disagrees() -> None:
    with _patches(25.0, 1.05, 505.0, 515.0):
        await im.get_market_regime()
    # 他處只重設 regime 快取、快照殘留且 regime 不一致 → None
    im._market_regime_cache_value = "NORMAL"
    im._market_regime_cache_expiry = time.time() + 60.0
    assert await im.get_market_regime_inputs() is None


async def test_redline_inputs_none_when_snapshot_stale() -> None:
    with _patches(25.0, 1.05, 505.0, 515.0):
        await im.get_market_regime()
    snap = im._market_regime_inputs_value
    assert snap is not None
    im._market_regime_inputs_value = im.MarketRegimeInputs(
        regime=snap.regime,
        vix=snap.vix,
        vts_ratio=snap.vts_ratio,
        spy_spot=snap.spy_spot,
        gamma_flip=snap.gamma_flip,
        computed_at=time.time() - im._MARKET_REGIME_CACHE_TTL - 5.0,
    )
    assert await im.get_market_regime_inputs() is None
