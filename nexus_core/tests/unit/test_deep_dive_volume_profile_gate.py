"""`/x` 深度分析 Volume Profile 的 Yahoo 閘門節流（`_throttled_volume_profile`）。"""

from __future__ import annotations

from contextlib import asynccontextmanager
from typing import Any, AsyncIterator
from unittest.mock import patch

import pytest

from cogs.unified_terminal.symbol_deep_dive import _throttled_volume_profile
from services import market_data_service
from services.rate_gate import get_gate

_VP = {"poc": 100.0, "hvn": 105.0, "lvn": 95.0}


@pytest.mark.asyncio
async def test_volume_profile_goes_through_yahoo_gate() -> None:
    """正常路徑：佔用一個 Yahoo 名額執行，回傳結果並歸還名額。"""
    gate = get_gate("yahoo")
    with patch(
        "market_analysis.volume_profile.calculate_volume_profile", return_value=_VP
    ) as mock_vp:
        result = await _throttled_volume_profile("AAPL")

    assert result == _VP
    mock_vp.assert_called_once_with("AAPL")
    assert gate.drain_stats().granted == 1
    assert gate.in_flight() == 0


@pytest.mark.asyncio
async def test_volume_profile_interactive_lane_under_x_command() -> None:
    """在 /x 互動標記下走互動通道：背景併發滿載時仍可取得名額。"""
    gate = get_gate("yahoo")
    # 先佔滿背景併發（yahoo 背景上限 2），互動請求仍須立即核發
    bg_tickets = [await gate.acquire() for _ in range(gate.policy.bg_concurrency)]
    try:
        with (
            patch(
                "market_analysis.volume_profile.calculate_volume_profile",
                return_value=_VP,
            ),
            market_data_service.mark_interactive_request(),
        ):
            result = await _throttled_volume_profile("AAPL")
    finally:
        for t in bg_tickets:
            gate.release(t)

    assert result == _VP
    assert gate.drain_stats().granted == len(bg_tickets) + 1
    assert gate.in_flight() == 0


@pytest.mark.asyncio
async def test_volume_profile_returns_none_in_yahoo_cooldown() -> None:
    """Yahoo 冷卻中：快速熔斷回 None，不送出 yfinance 請求、不消耗配額。"""
    gate = get_gate("yahoo")
    gate.trip()
    gate.drain_stats()  # 清掉 trip 計入的冷卻次數，只看核發
    with patch(
        "market_analysis.volume_profile.calculate_volume_profile", return_value=_VP
    ) as mock_vp:
        result = await _throttled_volume_profile("AAPL")

    assert result is None
    mock_vp.assert_not_called()
    assert gate.drain_stats().granted == 0


@pytest.mark.asyncio
async def test_volume_profile_returns_none_when_edge_busy() -> None:
    """排隊逾時／佇列滿載（YahooEdgeBusyError）：回 None，不中斷 gather。"""

    @asynccontextmanager
    async def _busy_slot() -> AsyncIterator[None]:
        raise market_data_service.YahooEdgeBusyError("排隊逾時")
        yield  # pragma: no cover

    with (
        patch.object(market_data_service, "yahoo_slot", _busy_slot),
        patch(
            "market_analysis.volume_profile.calculate_volume_profile", return_value=_VP
        ) as mock_vp,
    ):
        result = await _throttled_volume_profile("AAPL")

    assert result is None
    mock_vp.assert_not_called()


@pytest.mark.asyncio
async def test_volume_profile_releases_slot_when_calculation_raises() -> None:
    """計算本身拋出非閘門例外：照常外拋，但名額必須歸還。"""
    gate = get_gate("yahoo")

    def _boom(_symbol: str) -> Any:
        raise RuntimeError("boom")

    with patch(
        "market_analysis.volume_profile.calculate_volume_profile", side_effect=_boom
    ):
        with pytest.raises(RuntimeError, match="boom"):
            await _throttled_volume_profile("AAPL")

    assert gate.in_flight() == 0
