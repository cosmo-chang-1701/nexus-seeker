"""低頻外部來源（FRED／TSA／TWSE／TPEx／Polymarket／Alpaca REST）接上 rate_gate：
HTTP 429 → 該來源冷卻 → 後續請求快速熔斷（不送出）→ 呼叫端既有 fail-safe（回空／None）。"""

from __future__ import annotations

import json
from datetime import date
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from services import api_budget, rate_gate


@pytest.fixture(autouse=True)
def _reset_budget() -> None:
    api_budget.reset_for_tests()


def _real_resp(
    status: int, text: str = "", retry_after: str | None = None
) -> httpx.Response:
    headers = {"Retry-After": retry_after} if retry_after else {}
    return httpx.Response(
        status,
        text=text,
        headers=headers,
        request=httpx.Request("GET", "http://source.test/"),
    )


# ---------------------------------------------------------------------------
# FRED
# ---------------------------------------------------------------------------
async def test_fred_429_cools_down_and_alt_data_returns_failure_reason() -> None:
    from services.alt_data_service import AltDataService

    svc = AltDataService()
    with patch(
        "httpx.AsyncClient.get",
        new=AsyncMock(return_value=_real_resp(429, retry_after="90")),
    ) as m_get:
        res, reason = await svc.get_fred_period_yoy(
            "TOTALSA", "2026-Q2", date(2026, 7, 15)
        )
        assert res is None
        assert "抓取失敗" in reason
        assert rate_gate.get_gate("fred").in_cooldown()
        assert m_get.await_count == 1

        # 冷卻中：不再送出請求，仍走既有 fail-safe
        res2, reason2 = await svc.get_fred_period_yoy(
            "PAYEMS", "2026-Q2", date(2026, 7, 15)
        )
        assert res2 is None and "抓取失敗" in reason2
        assert m_get.await_count == 1
    assert api_budget.snapshot()["fred/fredgraph/429"] == 1


async def test_fred_success_is_counted_by_gate() -> None:
    from services.macro_signal_service import _download_fred

    csv_text = "DATE,X\n2026-01-01,1.5\n"
    with patch(
        "httpx.AsyncClient.get",
        new=AsyncMock(return_value=_real_resp(200, csv_text)),
    ):
        assert (await _download_fred("TOTALSA", date(2026, 1, 1))).startswith("DATE")
    assert api_budget.snapshot()["fred/fredgraph/background"] == 1
    assert rate_gate.get_gate("fred").drain_stats().granted == 1


# ---------------------------------------------------------------------------
# TSA／TWSE／TPEx：各自獨立閘門，不再共用 Semaphore(3)
# ---------------------------------------------------------------------------
async def test_tsa_429_cools_down_tsa_only_and_returns_empty() -> None:
    from services.alt_data_service import AltDataService

    svc = AltDataService()
    assert not hasattr(svc, "_semaphore")
    with patch(
        "httpx.AsyncClient.get",
        new=AsyncMock(return_value=_real_resp(429, retry_after="60")),
    ) as m_get:
        assert await svc.fetch_tsa_daily(2026, today=date(2026, 7, 15)) == {}
        assert rate_gate.get_gate("tsa").in_cooldown()
        assert not rate_gate.get_gate("twse").in_cooldown()
        assert not rate_gate.get_gate("fred").in_cooldown()
        assert await svc.fetch_tsa_daily(2025, today=date(2026, 7, 15)) == {}
        assert m_get.await_count == 1  # 冷卻中不送出


async def test_twse_and_tpex_have_independent_gates() -> None:
    from services.alt_data_service import AltDataService

    svc = AltDataService()

    async def _get(url: str, *a: Any, **k: Any) -> httpx.Response:
        if "twse.com.tw" in url:
            return _real_resp(429, retry_after="60")
        return _real_resp(200, "[]")

    with patch("httpx.AsyncClient.get", new=AsyncMock(side_effect=_get)):
        assert await svc.fetch_twse_monthly_revenues() == {}
        assert rate_gate.get_gate("twse").in_cooldown()
        assert not rate_gate.get_gate("tpex").in_cooldown()
        assert await svc.fetch_tpex_monthly_revenues() == {}
    snap = api_budget.snapshot()
    assert snap["twse/monthly_revenue/429"] == 1
    assert snap["tpex/monthly_revenue/background"] == 1


# ---------------------------------------------------------------------------
# Polymarket
# ---------------------------------------------------------------------------
async def test_polymarket_order_book_init_cooldown_stops_requests_without_sleep() -> (
    None
):
    from services.polymarket_service import PolymarketService

    svc = PolymarketService(MagicMock())
    with (
        patch(
            "httpx.AsyncClient.get",
            new=AsyncMock(return_value=_real_resp(429, retry_after="60")),
        ) as m_get,
        patch("asyncio.sleep", new_callable=AsyncMock) as m_sleep,
    ):
        await svc._initialize_order_books(["a", "b", "c"])
    assert m_get.await_count == 1  # 第一次 429 後其餘快速熔斷
    m_sleep.assert_not_called()  # 不再用 sleep(0.1) 節流（改由 min_interval）
    assert rate_gate.get_gate("polymarket").in_cooldown()


async def test_polymarket_search_falls_back_to_local_results_during_cooldown() -> None:
    from services.polymarket_service import PolymarketService

    svc = PolymarketService(MagicMock())
    rate_gate.get_gate("polymarket").trip(retry_after=60)
    with patch("httpx.AsyncClient.get", new_callable=AsyncMock) as m_get:
        assert await svc.search_markets("fed rate", limit=3) == []
        m_get.assert_not_called()


async def test_sector_runner_poly_events_empty_during_cooldown() -> None:
    from market_analysis.analyst_runners.sector_runner import _fetch_poly_events

    with patch(
        "httpx.AsyncClient.get",
        new=AsyncMock(return_value=_real_resp(429, retry_after="60")),
    ) as m_get:
        assert await _fetch_poly_events(MagicMock()) == []
        assert rate_gate.get_gate("polymarket").in_cooldown()
        assert await _fetch_poly_events(MagicMock()) == []
        assert m_get.await_count == 1


def test_polymarket_gate_replaces_manual_sleep_with_min_interval() -> None:
    assert rate_gate.POLICIES["polymarket"].min_interval == pytest.approx(0.1)


# ---------------------------------------------------------------------------
# Alpaca REST
# ---------------------------------------------------------------------------
async def test_alpaca_historical_bars_429_returns_none_then_fast_circuit() -> None:
    from datetime import datetime, timezone

    from services.alpaca_stream_service import AlpacaStreamService

    svc = AlpacaStreamService()
    start = datetime(2026, 7, 1, tzinfo=timezone.utc)
    end = datetime(2026, 7, 2, tzinfo=timezone.utc)
    with patch(
        "httpx.AsyncClient.get",
        new=AsyncMock(return_value=_real_resp(429, retry_after="60")),
    ) as m_get:
        assert await svc.fetch_historical_bars(["AAPL"], "15Min", start, end) is None
        assert rate_gate.get_gate("alpaca_rest").in_cooldown()
        assert await svc.fetch_historical_bars(["AAPL"], "15Min", start, end) is None
        assert m_get.await_count == 1


async def test_alpaca_historical_bars_paginates_through_gate() -> None:
    from datetime import datetime, timezone

    from services.alpaca_stream_service import AlpacaStreamService

    svc = AlpacaStreamService()
    pages = [
        {"bars": {"AAPL": [{"t": "x"}]}, "next_page_token": "tok"},
        {"bars": {"AAPL": [{"t": "y"}]}, "next_page_token": None},
    ]
    resps = [_real_resp(200, json.dumps(p)) for p in pages]
    start = datetime(2026, 7, 1, tzinfo=timezone.utc)
    end = datetime(2026, 7, 2, tzinfo=timezone.utc)
    with patch("httpx.AsyncClient.get", new=AsyncMock(side_effect=resps)):
        out = await svc.fetch_historical_bars(["AAPL"], "15Min", start, end)
    assert out == {"AAPL": [{"t": "x"}, {"t": "y"}]}
    assert rate_gate.get_gate("alpaca_rest").drain_stats().granted == 2
