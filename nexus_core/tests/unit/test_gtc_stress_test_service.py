"""GTC 壓力測試共用服務（/stress_test 與 /dash 壓測）單元測試。"""

from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest

from market_analysis.dynamic_rollover._shared import count_gtc_buy_orders
from services.gtc_stress_test_service import (
    build_stress_test_results,
    run_gtc_stress_test,
)


def _gtc_buy(price: float, qty: float, **over: Any) -> dict[str, Any]:
    d: dict[str, Any] = {
        "validity": "GTC",
        "side": "BUY",
        "limit_price": price,
        "stop_price": 0.0,
        "quantity": qty,
    }
    d.update(over)
    return d


def test_stress_no_180_share_cap() -> None:
    res = build_stress_test_results(
        [_gtc_buy(22500.0, 1.0)], 150.0, [("BOXX", 850.0, 116.0)]
    )
    assert res["cash_equiv_value"] == pytest.approx(98600.0)
    assert res["is_critical"] is False
    assert res["net_deficit"] == pytest.approx(150.0 + 98600.0 - 22500.0)


def test_stress_multiple_equivalents_and_order_filter() -> None:
    orders = [
        _gtc_buy(10.0, 100.0),
        _gtc_buy(0.0, 10.0, stop_price=5.0),
        {**_gtc_buy(10.0, 100.0), "validity": "DAY"},
        {**_gtc_buy(10.0, 100.0), "side": "SELL"},
    ]
    res = build_stress_test_results(
        orders, 0.0, [("BOXX", 10.0, 100.0), ("SGOV", 20.0, 100.0)]
    )
    assert res["total_deficit"] == pytest.approx(1050.0)
    assert res["gtc_buy_orders_count"] == 2
    assert res["cash_equiv_value"] == pytest.approx(3000.0)
    assert res["cash_equiv_detail"] == "BOXX 10 股、SGOV 20 股"


def test_stress_missing_price_degrades() -> None:
    res = build_stress_test_results(
        [_gtc_buy(1000.0, 10.0)], 0.0, [("BOXX", 10.0, None)]
    )
    assert res["cash_equiv_value"] is None
    assert res["net_deficit"] is None
    assert res["is_critical"] is False


def test_stress_critical_when_buffer_short() -> None:
    res = build_stress_test_results(
        [_gtc_buy(1000.0, 30.0)], 150.0, [("BOXX", 100.0, 116.0)]
    )
    assert res["is_critical"] is True
    assert res["net_deficit"] < 0


def _patches(
    orders: Any = None,
    ctx: Any = None,
    holdings: Any = None,
    quote: Any = None,
    runway: Any = None,
) -> list[Any]:
    return [
        patch(
            "database.orders.get_user_active_orders",
            **(
                {"side_effect": orders}
                if isinstance(orders, Exception)
                else {"return_value": orders or []}
            ),
        ),
        patch(
            "database.get_full_user_context",
            return_value=ctx or SimpleNamespace(cash_reserve=100.0, load_failed=False),
        ),
        patch("database.holdings.get_user_holdings", return_value=holdings or []),
        patch("services.market_data_service.get_quote", new=quote or AsyncMock()),
        patch(
            "services.withdrawal_runway_service.get_runway_display",
            new=runway or AsyncMock(return_value=(None, False)),
        ),
    ]


async def _run(**kw: Any) -> dict[str, Any]:
    ps = _patches(**kw)
    for p in ps:
        p.start()
    try:
        return await run_gtc_stress_test(1)
    finally:
        for p in ps:
            p.stop()


@pytest.mark.asyncio
async def test_stress_run_load_failed_fail_closed() -> None:
    res = await _run(ctx=SimpleNamespace(cash_reserve=0.0, load_failed=True))
    assert res["cash_reserve"] is None
    assert res["load_failed"] is True
    assert res["is_critical"] is False


@pytest.mark.asyncio
async def test_stress_run_runway_passthrough() -> None:
    snap = SimpleNamespace(next_withdrawal=10000.0, next_date="2027-01-04")
    fresh = await _run(runway=AsyncMock(return_value=(snap, False)))
    assert fresh["next_withdrawal"] == 10000.0
    assert fresh["next_withdrawal_date"] == "2027-01-04"
    assert fresh["next_withdrawal_stale"] is False
    stale = await _run(runway=AsyncMock(return_value=(snap, True)))
    assert stale["next_withdrawal_stale"] is True
    none = await _run()
    assert none["next_withdrawal"] is None
    assert none["next_withdrawal_date"] is None


@pytest.mark.asyncio
async def test_stress_run_orders_read_failure_treated_as_empty() -> None:
    res = await _run(orders=RuntimeError("db"))
    assert res["total_deficit"] == 0.0
    assert res["gtc_buy_orders_count"] == 0


@pytest.mark.asyncio
async def test_stress_run_quote_raises_degrades() -> None:
    res = await _run(
        holdings=[{"symbol": "BOXX", "quantity": 10.0}],
        quote=AsyncMock(side_effect=RuntimeError("net")),
    )
    assert res["cash_equiv_value"] is None


@pytest.mark.asyncio
async def test_stress_run_live_quote_used() -> None:
    res = await _run(
        holdings=[
            {"symbol": "boxx", "quantity": 850.0},
            {"symbol": "AAPL", "quantity": 5.0},
        ],
        quote=AsyncMock(return_value={"c": 116.0}),
    )
    assert res["cash_equiv_value"] == pytest.approx(98600.0)


@pytest.mark.asyncio
async def test_stress_run_runway_raises_degrades() -> None:
    res = await _run(runway=AsyncMock(side_effect=RuntimeError("x")))
    assert res["next_withdrawal"] is None
    assert res["next_withdrawal_date"] is None


def test_stress_count_gtc_buy_orders() -> None:
    orders = [
        _gtc_buy(0.0, 1.0),
        {**_gtc_buy(1.0, 1.0), "validity": "DAY"},
        {**_gtc_buy(1.0, 1.0), "side": "SELL"},
    ]
    assert count_gtc_buy_orders(orders) == 1
