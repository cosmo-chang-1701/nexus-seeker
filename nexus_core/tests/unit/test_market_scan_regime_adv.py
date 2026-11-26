"""NRO 掃描 DYNAMIC Regime 分類注入 `adv_dollar_20d`（與雷達／顧問路徑同公式）。"""

from typing import Any, Iterator
from unittest.mock import AsyncMock, patch

import pandas as pd
import pytest

from market_analysis.dynamic_rollover.models import DynamicRegime
from services.trading_service.market_scan import _classify_symbol_regime_value
from tests.unit.network_guard import NetworkGuard

_SPOT = 150.0
_GEX: dict[str, Any] = {
    "put_wall": 140.0,
    "call_wall": 160.0,
    "net_gex": 1.0,
    "gex_profile": {"140.0": 1_000_000.0},
}
_REGIME = DynamicRegime.REGIME_V_BREAKDOWN_CHASE


@pytest.fixture(autouse=True)
def _no_outbound_network(monkeypatch: pytest.MonkeyPatch) -> Iterator[NetworkGuard]:
    """攔截所有對外連線；即使呼叫端吞掉例外，teardown 仍會讓測試失敗。"""
    guard = NetworkGuard()
    guard.install(monkeypatch)
    yield guard
    guard.assert_clean()


@pytest.fixture
def gex_mock() -> Iterator[AsyncMock]:
    with patch(
        "market_analysis.index_microstructure.fetch_symbol_gex_metrics",
        new_callable=AsyncMock,
        return_value=dict(_GEX),
    ) as mock:
        yield mock


@pytest.fixture
def history_mock() -> Iterator[AsyncMock]:
    with patch(
        "services.market_data_service.get_history_df",
        new_callable=AsyncMock,
        return_value=pd.DataFrame(),
    ) as mock:
        yield mock


@pytest.fixture
def classify_mock() -> Iterator[AsyncMock]:
    with patch(
        "market_analysis.dynamic_rollover.regime_classifier.classify_dynamic_regime",
        new_callable=AsyncMock,
        return_value=(_REGIME, "測試", None),
    ) as mock:
        yield mock


def _passed_profile(classify_mock: AsyncMock) -> dict[str, Any]:
    classify_mock.assert_awaited_once()
    assert classify_mock.await_args is not None
    profile = classify_mock.await_args.args[2]
    assert isinstance(profile, dict)
    return profile


@pytest.mark.asyncio
async def test_adv_dollar_20d_injected(
    gex_mock: AsyncMock, history_mock: AsyncMock, classify_mock: AsyncMock
) -> None:
    # 前 5 日量能刻意放大：只有最近 20 日應納入均量
    history_mock.return_value = pd.DataFrame(
        {"Close": [100.0] * 25, "Volume": [9_000_000.0] * 5 + [2_000_000.0] * 20}
    )

    assert await _classify_symbol_regime_value("AAPL", _SPOT) == _REGIME.value

    history_mock.assert_awaited_once_with("AAPL", period="1y", interval="1d")
    profile = _passed_profile(classify_mock)
    assert profile["adv_dollar_20d"] == 2_000_000.0 * _SPOT
    assert profile["put_wall"] == _GEX["put_wall"]
    assert profile["gex_profile"] == _GEX["gex_profile"]


@pytest.mark.asyncio
async def test_zero_spot_falls_back_to_daily_close(
    gex_mock: AsyncMock, history_mock: AsyncMock, classify_mock: AsyncMock
) -> None:
    # 最後一根 Close 刻意與先前不同：ADV 須以日線最後收盤價計算
    history_mock.return_value = pd.DataFrame(
        {"Close": [90.0] * 24 + [120.0], "Volume": [2_000_000.0] * 25}
    )

    assert await _classify_symbol_regime_value("AAPL", 0.0) == _REGIME.value

    profile = _passed_profile(classify_mock)
    assert profile["adv_dollar_20d"] == 2_000_000.0 * 120.0
    # 備援價只用於 ADV，傳給分類器的 spot 維持原值
    assert classify_mock.await_args is not None
    assert classify_mock.await_args.args[1] == 0.0


@pytest.mark.asyncio
async def test_empty_history_omits_adv_and_still_classifies(
    gex_mock: AsyncMock, history_mock: AsyncMock, classify_mock: AsyncMock
) -> None:
    assert await _classify_symbol_regime_value("AAPL", _SPOT) == _REGIME.value

    history_mock.assert_awaited_once()
    assert "adv_dollar_20d" not in _passed_profile(classify_mock)


@pytest.mark.asyncio
async def test_history_error_fails_closed(
    gex_mock: AsyncMock, history_mock: AsyncMock, classify_mock: AsyncMock
) -> None:
    history_mock.side_effect = RuntimeError("日線抓取失敗")

    assert await _classify_symbol_regime_value("AAPL", _SPOT) is None

    classify_mock.assert_not_awaited()


@pytest.mark.asyncio
async def test_empty_gex_skips_history_fetch(
    gex_mock: AsyncMock, history_mock: AsyncMock, classify_mock: AsyncMock
) -> None:
    gex_mock.return_value = {}

    assert await _classify_symbol_regime_value("AAPL", _SPOT) is None

    history_mock.assert_not_awaited()
    classify_mock.assert_not_awaited()
