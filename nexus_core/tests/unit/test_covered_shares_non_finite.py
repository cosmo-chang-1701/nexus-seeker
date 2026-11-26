"""既有空 Call 的 quantity 非有限值或無法轉成數值時的 fail-closed 測試。"""

import json
from typing import Any, Iterator
from unittest.mock import AsyncMock, patch

import pytest

from market_analysis.trading_orchestration import (
    get_covered_shares,
    recommend_covered_calls,
)
from tests.unit.network_guard import NetworkGuard

_MODULE = "market_analysis.trading_orchestration"

# JSON 解析可產生的非有限值，以及無法轉成數值的型別 (含超出 float 範圍的整數)。
_BAD_QUANTITIES: list[Any] = [
    json.loads("NaN"),
    json.loads("Infinity"),
    json.loads("-Infinity"),
    "abc",
    {},
    10**400,
]


@pytest.fixture(autouse=True)
def _no_outbound_network(monkeypatch: pytest.MonkeyPatch) -> Iterator[NetworkGuard]:
    """攔截所有對外連線；即使呼叫端吞掉例外，teardown 仍會讓測試失敗。"""
    guard = NetworkGuard()
    guard.install(monkeypatch)
    yield guard
    guard.assert_clean()


def _row(symbol: str, opt_type: str, quantity: Any) -> tuple[Any, ...]:
    return (
        1,
        symbol,
        opt_type,
        160.0,
        "2026-07-20",
        5.0,
        quantity,
        150.0,
        0.0,
        0.0,
        0.0,
        "HEDGE",
    )


@pytest.mark.parametrize("quantity", _BAD_QUANTITIES)
def test_get_covered_shares_rejects_invalid_call_quantity(quantity: Any) -> None:
    rows = [_row("AAPL", "call", -1), _row("AAPL", "CALL", quantity)]
    with patch("database.portfolio.get_user_portfolio", return_value=rows):
        with pytest.raises(ValueError, match="AAPL 的 call 部位 quantity 無效"):
            get_covered_shares(1, "aapl")


def test_get_covered_shares_ignores_bad_quantity_on_unrelated_rows() -> None:
    nan = float("nan")
    rows = [
        _row("MSFT", "call", nan),
        _row("AAPL", "put", nan),
        _row("AAPL", "call", None),
        _row("AAPL", "call", -1),
    ]
    with patch("database.portfolio.get_user_portfolio", return_value=rows):
        covered_shares, existing_calls = get_covered_shares(1, "AAPL")

    assert covered_shares == 100.0
    assert len(existing_calls) == 1


def test_get_covered_shares_converts_numeric_string() -> None:
    rows = [_row("AAPL", "call", "-2")]
    with patch("database.portfolio.get_user_portfolio", return_value=rows):
        covered_shares, existing_calls = get_covered_shares(1, "AAPL")

    assert covered_shares == 200.0
    assert existing_calls[0]["quantity"] == -2.0
    assert existing_calls[0]["shares_covered"] == 200.0


@pytest.mark.asyncio
@pytest.mark.parametrize("quantity", _BAD_QUANTITIES)
async def test_recommend_covered_calls_returns_none_on_invalid_quantity(
    quantity: Any,
) -> None:
    holdings = [{"symbol": "AAPL", "quantity": 300.0, "avg_cost": 150.0}]
    with (
        patch(
            f"{_MODULE}.is_covered_call_unlock_allowed",
            new_callable=AsyncMock,
            return_value=True,
        ),
        patch(f"{_MODULE}.get_user_holdings", return_value=holdings),
        patch(
            "database.portfolio.get_user_portfolio",
            return_value=[_row("AAPL", "call", quantity)],
        ),
        patch(f"{_MODULE}.get_user_active_orders", return_value=[]) as mock_orders,
        patch(f"{_MODULE}.get_quote", new_callable=AsyncMock) as mock_quote,
    ):
        result = await recommend_covered_calls(1, "AAPL")

    assert result is None
    mock_orders.assert_not_called()
    mock_quote.assert_not_awaited()
