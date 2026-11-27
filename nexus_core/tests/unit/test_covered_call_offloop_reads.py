"""Covered Call 推薦與解鎖閘門的 SQLite 讀取須離開 event loop，且行為不變。"""

import logging
import threading
from typing import Any, Callable, Iterator
from unittest.mock import AsyncMock, patch

import pytest

from market_analysis.trading_orchestration import (
    _load_covered_call_inputs,
    filter_cc_recovery_targets,
    is_covered_call_unlock_allowed,
    recommend_covered_calls,
)
from tests.unit.network_guard import NetworkGuard

_MODULE = "market_analysis.trading_orchestration"
_HOLDINGS = [{"symbol": "AAPL", "quantity": 300.0, "avg_cost": 150.0}]


@pytest.fixture(autouse=True)
def _no_outbound_network(monkeypatch: pytest.MonkeyPatch) -> Iterator[NetworkGuard]:
    """攔截所有對外連線；即使呼叫端吞掉例外，teardown 仍會讓測試失敗。"""
    guard = NetworkGuard()
    guard.install(monkeypatch)
    yield guard
    guard.assert_clean()


@pytest.fixture
def unlocked() -> Iterator[AsyncMock]:
    with patch(
        f"{_MODULE}.is_covered_call_unlock_allowed",
        new_callable=AsyncMock,
        return_value=True,
    ) as mock:
        yield mock


def _record_thread(calls: dict[str, int], name: str, result: Any) -> Callable[..., Any]:
    def _side_effect(*_args: Any) -> Any:
        calls[name] = threading.get_ident()
        return result

    return _side_effect


@pytest.mark.asyncio
async def test_recommend_covered_calls_reads_off_event_loop(
    unlocked: AsyncMock,
) -> None:
    loop_thread = threading.get_ident()
    calls: dict[str, int] = {}
    with (
        patch(
            f"{_MODULE}.get_user_holdings",
            side_effect=_record_thread(calls, "holdings", _HOLDINGS),
        ),
        patch(
            f"{_MODULE}.get_covered_shares",
            side_effect=_record_thread(calls, "covered", (100.0, [])),
        ),
        patch(
            f"{_MODULE}.get_user_active_orders",
            side_effect=_record_thread(calls, "orders", []),
        ),
        patch(
            f"{_MODULE}.SentimentEngine.get_last_stored_iv",
            side_effect=_record_thread(calls, "iv", 0.30),
        ),
        patch(
            f"{_MODULE}.get_quote",
            new_callable=AsyncMock,
            return_value={"c": 148.0},
        ),
        patch(
            f"{_MODULE}.get_all_option_expiries",
            new_callable=AsyncMock,
            return_value=[],
        ),
    ):
        result = await recommend_covered_calls(1, "aapl")

    assert set(calls) == {"holdings", "covered", "orders", "iv"}
    assert loop_thread not in calls.values(), f"仍有讀取在 event loop 執行緒: {calls}"
    assert result is not None
    assert result["uncovered_shares"] == 200.0
    assert result["max_new_contracts"] == 2
    assert result["fallback_iv"] == 0.30


@pytest.mark.asyncio
async def test_unlock_gate_kv_reads_off_event_loop() -> None:
    loop_thread = threading.get_ident()
    calls: dict[str, int] = {}
    values = {"macro_sahm_rule": 0.35, "macro_us10y": 4.25}

    def _kv_fresh(key: str, *_args: Any, **_kwargs: Any) -> Any:
        calls[key] = threading.get_ident()
        return values.get(key)

    with (
        patch("database.get_kv_cache_fresh", side_effect=_kv_fresh),
        patch(
            "services.market_data_service.get_quote",
            new_callable=AsyncMock,
            return_value=None,
        ),
        patch(
            "services.market_data_service.get_vix_spot_strict",
            new_callable=AsyncMock,
            return_value=18.0,
        ),
    ):
        assert await is_covered_call_unlock_allowed() is True

    assert set(calls) == {"macro_sahm_rule", "macro_us10y"}
    assert loop_thread not in calls.values(), f"仍有讀取在 event loop 執行緒: {calls}"


@pytest.mark.asyncio
async def test_holdings_error_still_propagates(unlocked: AsyncMock) -> None:
    with (
        patch(f"{_MODULE}.get_user_holdings", side_effect=ValueError("持股壞資料")),
        patch(f"{_MODULE}.get_covered_shares") as mock_covered,
    ):
        with pytest.raises(ValueError, match="持股壞資料"):
            await recommend_covered_calls(1, "AAPL")

    mock_covered.assert_not_called()


@pytest.mark.asyncio
async def test_orders_error_still_propagates(unlocked: AsyncMock) -> None:
    with (
        patch(f"{_MODULE}.get_user_holdings", return_value=_HOLDINGS),
        patch(f"{_MODULE}.get_covered_shares", return_value=(0.0, [])),
        patch(
            f"{_MODULE}.get_user_active_orders",
            side_effect=ValueError("掛單壞資料"),
        ),
        patch(f"{_MODULE}.get_quote", new_callable=AsyncMock) as mock_quote,
    ):
        with pytest.raises(ValueError, match="掛單壞資料"):
            await recommend_covered_calls(1, "AAPL")

    mock_quote.assert_not_awaited()


@pytest.mark.asyncio
async def test_covered_read_error_logs_position_wording(
    unlocked: AsyncMock, caplog: pytest.LogCaptureFixture
) -> None:
    with (
        patch(f"{_MODULE}.get_user_holdings", return_value=_HOLDINGS),
        patch(
            f"{_MODULE}.get_covered_shares",
            side_effect=ValueError("部位壞資料"),
        ),
        patch(f"{_MODULE}.get_user_active_orders") as mock_orders,
        patch(f"{_MODULE}.get_quote", new_callable=AsyncMock) as mock_quote,
        caplog.at_level(logging.WARNING, logger=_MODULE),
    ):
        result = await recommend_covered_calls(1, "AAPL")

    assert result is None
    assert "既有部位讀取失敗" in caplog.text
    assert "部位壞資料" in caplog.text
    mock_orders.assert_not_called()
    mock_quote.assert_not_awaited()


@pytest.mark.asyncio
async def test_lazy_reads_preserved() -> None:
    # 報價無效時不讀儲存的 IV
    with (
        patch(
            f"{_MODULE}.is_covered_call_unlock_allowed",
            new_callable=AsyncMock,
            return_value=True,
        ),
        patch(f"{_MODULE}.get_user_holdings", return_value=_HOLDINGS),
        patch(f"{_MODULE}.get_covered_shares", return_value=(0.0, [])),
        patch(f"{_MODULE}.get_user_active_orders", return_value=[]),
        patch(
            f"{_MODULE}.get_quote",
            new_callable=AsyncMock,
            return_value={"c": 0.0},
        ),
        patch(f"{_MODULE}.SentimentEngine.get_last_stored_iv") as mock_iv,
    ):
        assert await recommend_covered_calls(1, "AAPL") is None

    mock_iv.assert_not_called()

    # ^TNX 即時報價可用時不退回讀 macro_us10y 快取
    with (
        patch("database.get_kv_cache_fresh", return_value=0.35) as mock_kv,
        patch(
            "services.market_data_service.get_quote",
            new_callable=AsyncMock,
            return_value={"c": 4.25},
        ),
        patch(
            "services.market_data_service.get_vix_spot_strict",
            new_callable=AsyncMock,
            return_value=18.0,
        ),
    ):
        assert await is_covered_call_unlock_allowed() is True

    read_keys = [call.args[0] for call in mock_kv.call_args_list]
    assert read_keys == ["macro_sahm_rule"]


@pytest.mark.asyncio
async def test_filter_cc_recovery_reads_stored_iv_off_event_loop() -> None:
    loop_thread = threading.get_ident()
    calls: dict[str, int] = {}
    with (
        patch(
            "database.market_cache.get_market_cache",
            return_value={"reference_spot_price": 100.0},
        ),
        patch(
            f"{_MODULE}.SentimentEngine.get_last_stored_iv",
            side_effect=_record_thread(calls, "iv", 0.30),
        ),
        patch(
            f"{_MODULE}.get_all_option_expiries",
            new_callable=AsyncMock,
            side_effect=RuntimeError("到期日讀取失敗"),
        ) as mock_expiries,
    ):
        result = await filter_cc_recovery_targets("aapl")

    assert result is None
    mock_expiries.assert_awaited_once_with("AAPL")
    assert set(calls) == {"iv"}
    assert loop_thread not in calls.values(), f"仍有讀取在 event loop 執行緒: {calls}"


def test_covered_error_stored_as_message() -> None:
    with (
        patch(f"{_MODULE}.get_user_holdings", return_value=_HOLDINGS),
        patch(f"{_MODULE}.get_covered_shares", side_effect=ValueError("壞資料")),
    ):
        inputs = _load_covered_call_inputs(1, "AAPL")

    assert inputs.covered_error == "壞資料"
    assert isinstance(inputs.covered_error, str)
