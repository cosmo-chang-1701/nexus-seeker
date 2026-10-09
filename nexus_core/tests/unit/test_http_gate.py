"""services/http_gate：經閘門送出 httpx 請求、429／SEC 403 冷卻、2xx 重置退避、slot 釋放。"""

from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from services import api_budget, rate_gate
from services.http_gate import gated_request, gated_stream


@pytest.fixture(autouse=True)
def _reset_budget() -> None:
    api_budget.reset_for_tests()


def _resp(
    status: int, body: str = "", headers: dict[str, str] | None = None
) -> MagicMock:
    resp = MagicMock()
    resp.status_code = status
    resp.headers = headers or {}
    resp.text = body
    resp.aread = AsyncMock(return_value=body.encode())
    return resp


def _client(resp: MagicMock) -> Any:
    client = MagicMock()
    client.get = AsyncMock(return_value=resp)
    cm = AsyncMock()
    cm.__aenter__.return_value = resp
    cm.__aexit__.return_value = None
    client.stream = MagicMock(return_value=cm)
    return client


async def test_success_returns_response_counts_call_and_releases_slot() -> None:
    resp = _resp(200)
    client = _client(resp)
    out = await gated_request("fred", client, "GET", "u", endpoint="series", x=1)
    assert out is resp
    client.get.assert_awaited_once_with("u", x=1)
    assert api_budget.snapshot() == {"fred/series/background": 1}
    assert rate_gate.get_gate("fred").in_flight() == 0


async def test_interactive_flag_is_recorded() -> None:
    client = _client(_resp(200))
    with rate_gate.mark_interactive_request():
        await gated_request("fred", client, "GET", "u", endpoint="series")
    assert api_budget.snapshot() == {"fred/series/interactive": 1}


async def test_429_trips_with_retry_after_and_blocks_next_call() -> None:
    client = _client(_resp(429, headers={"Retry-After": "90"}))
    out = await gated_request("tsa", client, "GET", "u", endpoint="page")
    assert out.status_code == 429  # 回應原樣回傳
    gate = rate_gate.get_gate("tsa")
    assert gate.in_cooldown()
    assert gate.cooldown_remaining() == pytest.approx(90, abs=1)
    assert api_budget.snapshot()["tsa/page/429"] == 1
    with pytest.raises(rate_gate.RateGateCooldownError):
        await gated_request("tsa", client, "GET", "u", endpoint="page")
    assert client.get.await_count == 1
    assert gate.in_flight() == 0


async def test_non_sec_403_with_marker_is_not_rate_limit() -> None:
    client = _client(_resp(403, "Request Rate Threshold Exceeded"))
    await gated_request("fred", client, "GET", "u", endpoint="series")
    assert not rate_gate.get_gate("fred").in_cooldown()


async def test_sec_403_threshold_vs_other_403() -> None:
    other = _client(_resp(403, "Undeclared Automated Tool"))
    await gated_request("sec", other, "GET", "u", endpoint="submissions")
    assert not rate_gate.get_gate("sec").in_cooldown()

    limited = _client(_resp(403, "Request Rate Threshold Exceeded"))
    await gated_request("sec", limited, "GET", "u", endpoint="submissions")
    assert rate_gate.get_gate("sec").in_cooldown()


async def test_2xx_resets_backoff_after_cooldown(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    t = [1000.0]
    monkeypatch.setattr(rate_gate, "_clock", lambda: t[0])
    gate = rate_gate.get_gate("fred")
    gate.trip()
    t[0] += gate.policy.cooldown_initial + 1
    assert not gate.in_cooldown()
    await gated_request("fred", _client(_resp(200)), "GET", "u", endpoint="series")
    t[0] += 1
    gate.trip()
    assert gate.cooldown_remaining() == pytest.approx(gate.policy.cooldown_initial)


async def test_exception_from_client_releases_slot() -> None:
    client = MagicMock()
    client.get = AsyncMock(side_effect=ConnectionError("down"))
    with pytest.raises(ConnectionError):
        await gated_request("fred", client, "GET", "u", endpoint="series")
    assert rate_gate.get_gate("fred").in_flight() == 0


async def test_stream_holds_slot_during_body_and_trips_on_429() -> None:
    gate = rate_gate.get_gate("sec")
    client = _client(_resp(200))
    async with gated_stream("sec", client, "GET", "u", endpoint="document") as r:
        assert r.status_code == 200
        assert gate.in_flight() == 1  # 讀取本文期間仍佔用名額
    assert gate.in_flight() == 0

    limited = _client(_resp(429, headers={"Retry-After": "30"}))
    async with gated_stream("sec", limited, "GET", "u", endpoint="document"):
        pass
    assert gate.in_cooldown()
    assert api_budget.snapshot()["sec/document/429"] == 1
