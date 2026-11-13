"""GEX 降級備援新鮮度：即時 scrape 為空/失敗時，挑 kv 與 edge 中最新且 ≤96h 者。"""

import copy
import time
from contextlib import contextmanager
from typing import Any, Iterator, Optional
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from market_analysis.index_microstructure import (
    _scrape_symbol_gex_raw,
    fetch_symbol_gex_metrics,
)

HOUR = 3600.0


def _snap(tag: float) -> dict:
    return {
        "spot": tag,
        "net_gex": 1000.0,
        "call_wall": tag + 5,
        "put_wall": tag - 5,
        "gex_profile": {
            str(tag - 5): 200.0,
            str(tag): 500.0,
            str(tag + 5): -300.0,
        },
    }


def _edge(tag: float, age_h: float) -> dict:
    return {"data": _snap(tag), "age_seconds": age_h * HOUR}


def _client(
    status: int = 200, body: Optional[dict] = None, exc: Optional[Exception] = None
) -> MagicMock:
    resp = MagicMock()
    resp.status_code = status
    resp.json.return_value = body if body is not None else {}
    inst = AsyncMock()
    if exc is not None:
        inst.get.side_effect = exc
    else:
        inst.get.return_value = resp
    inst.__aenter__.return_value = inst
    inst.__aexit__.return_value = None
    return MagicMock(return_value=inst)


_EMPTY_LIVE = {
    "status": "success",
    "data": {"spot": 0, "gex_profile": {}, "expiry": None},
}


@contextmanager
def _env(
    client: MagicMock,
    edge: Optional[dict] = None,
    tunnel: str = "http://fake-tunnel",
) -> Iterator[AsyncMock]:
    save = AsyncMock()
    with (
        patch("config.TUNNEL_URL", tunnel),
        patch("httpx.AsyncClient", client),
        patch(
            "services.edge_cache_client.get_cached_gex",
            new_callable=AsyncMock,
            return_value=edge,
        ),
        patch("database.cache.save_kv_cache", save),
    ):
        yield save


def _assert_stale(res: dict, spot: float, age_h: float) -> None:
    assert res["spot"] == spot
    assert res["_is_stale_cache"] is True
    assert res["_data_ts"] == pytest.approx(time.time() - age_h * HOUR, abs=5.0)


@pytest.mark.asyncio
async def test_a_live_empty_prefers_newer_edge_over_old_kv() -> None:
    stale = _snap(214.0)
    with _env(_client(body=_EMPTY_LIVE), _edge(168.0, 10)):
        res = await _scrape_symbol_gex_raw("NVDA", stale, time.time() - 10 * 86400)
    _assert_stale(res, 168.0, 10)


@pytest.mark.asyncio
async def test_b_live_empty_prefers_newer_kv() -> None:
    with _env(_client(body=_EMPTY_LIVE), _edge(168.0, 10)):
        res = await _scrape_symbol_gex_raw("NVDA", _snap(200.0), time.time() - 2 * HOUR)
    _assert_stale(res, 200.0, 2)


@pytest.mark.asyncio
async def test_c_both_older_than_cap_returns_empty() -> None:
    with _env(_client(body=_EMPTY_LIVE), _edge(168.0, 100)):
        res = await _scrape_symbol_gex_raw(
            "NVDA", _snap(200.0), time.time() - 120 * HOUR
        )
    assert res["gex_profile"] == {}
    assert res["spot"] == 0.0
    assert "_is_stale_cache" not in res


@pytest.mark.asyncio
async def test_d_live_empty_does_not_save_or_mutate() -> None:
    stale = _snap(200.0)
    before = copy.deepcopy(stale)
    with _env(_client(body=_EMPTY_LIVE), None) as save:
        res = await _scrape_symbol_gex_raw("NVDA", stale, time.time() - HOUR)
    save.assert_not_called()
    assert stale == before
    assert res is not stale
    assert res["_is_stale_cache"] is True


@pytest.mark.asyncio
async def test_d2_live_empty_without_stale_not_cached() -> None:
    with _env(_client(body=_EMPTY_LIVE), None) as save:
        res = await _scrape_symbol_gex_raw("MRVL")
    save.assert_not_called()
    assert res["gex_profile"] == {}


@pytest.mark.asyncio
async def test_e_live_valid_saved_and_no_data_ts() -> None:
    body = {"status": "success", "data": _snap(150.0)}
    with _env(_client(body=body), _edge(168.0, 1)) as save:
        res = await _scrape_symbol_gex_raw("NVDA")
    assert res["spot"] == 150.0
    assert "_data_ts" not in res
    assert "_is_stale_cache" not in res
    env = save.call_args.args[1]
    assert env["data_timestamp"] == pytest.approx(time.time(), abs=5.0)


@pytest.mark.asyncio
async def test_f_nonforce_old_edge_newer_than_kv_returned_as_stale() -> None:
    kv = {"data": _snap(214.0), "timestamp": time.time() - 10 * 86400}
    with (
        _env(_client(body=_EMPTY_LIVE), _edge(168.0, 10)),
        patch("database.cache.get_kv_cache", return_value=kv),
        patch("asyncio.create_task") as ct,
    ):
        res = await fetch_symbol_gex_metrics("NVDA")
        for call in ct.call_args_list:
            call.args[0].close()
    _assert_stale(res, 168.0, 10)


@pytest.mark.asyncio
async def test_g_api_exception_uses_same_selection() -> None:
    with _env(_client(exc=RuntimeError("boom")), _edge(168.0, 10)):
        res = await _scrape_symbol_gex_raw(
            "NVDA", _snap(214.0), time.time() - 10 * 86400
        )
    _assert_stale(res, 168.0, 10)


@pytest.mark.asyncio
async def test_h_nonforce_kv_older_than_cap_no_edge_returns_empty() -> None:
    kv = {"data": _snap(214.0), "timestamp": time.time() - 5 * 86400}
    with (
        _env(_client(body=_EMPTY_LIVE), None),
        patch("database.cache.get_kv_cache", return_value=kv),
        patch("asyncio.create_task") as ct,
    ):
        res = await fetch_symbol_gex_metrics("NVDA")
        for call in ct.call_args_list:
            call.args[0].close()
    assert res["gex_profile"] == {}
    assert "_is_stale_cache" not in res


@pytest.mark.asyncio
async def test_i_tunnel_unset_uses_kv_with_data_ts() -> None:
    with _env(_client(), _edge(168.0, 1), tunnel=""):
        res = await _scrape_symbol_gex_raw("NVDA", _snap(200.0), time.time() - 2 * HOUR)
    _assert_stale(res, 200.0, 2)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "status,body",
    [(500, {}), (200, {"status": "error"})],
)
async def test_j_http_error_same_selection(status: int, body: dict[str, Any]) -> None:
    with _env(_client(status=status, body=body), _edge(168.0, 10)):
        res = await _scrape_symbol_gex_raw(
            "NVDA", _snap(214.0), time.time() - 10 * 86400
        )
    _assert_stale(res, 168.0, 10)


@pytest.mark.asyncio
async def test_force_live_empty_falls_back_to_edge() -> None:
    kv = {"data": _snap(214.0), "timestamp": time.time() - 10 * 86400}
    with (
        _env(_client(body=_EMPTY_LIVE), _edge(168.0, 10)),
        patch("database.cache.get_kv_cache", return_value=kv),
    ):
        res = await fetch_symbol_gex_metrics("NVDA", force_live=True)
    _assert_stale(res, 168.0, 10)


def _legacy_data(tag: float) -> dict:
    d = _snap(tag)
    d["_is_stale_cache"] = True
    return d


@pytest.mark.asyncio
async def test_legacy_restamped_kv_force_live_prefers_edge() -> None:
    kv = {
        "data": _legacy_data(214.0),
        "timestamp": time.time() - 0.5 * HOUR,
        "data_timestamp": time.time() - 0.5 * HOUR,
    }
    with (
        _env(_client(body=_EMPTY_LIVE), _edge(168.0, 11)),
        patch("database.cache.get_kv_cache", return_value=kv),
    ):
        res = await fetch_symbol_gex_metrics("NVDA", force_live=True)
    _assert_stale(res, 168.0, 11)


@pytest.mark.asyncio
async def test_legacy_restamped_kv_nonforce_not_fresh_shortcut() -> None:
    kv = {
        "data": _legacy_data(214.0),
        "timestamp": time.time() - 5 * 60,
        "data_timestamp": time.time() - 5 * 60,
    }
    with (
        _env(_client(body=_EMPTY_LIVE), None),
        patch("database.cache.get_kv_cache", return_value=kv),
        patch("asyncio.create_task") as ct,
    ):
        res = await fetch_symbol_gex_metrics("NVDA")
        for call in ct.call_args_list:
            call.args[0].close()
    assert res.get("spot") != 214.0
    assert res["gex_profile"] == {}


@pytest.mark.asyncio
async def test_legacy_guard_normal_fresh_kv_still_returned() -> None:
    data = _snap(200.0)
    kv = {
        "data": data,
        "timestamp": time.time() - 5 * 60,
        "data_timestamp": time.time() - 5 * 60,
    }
    with (
        _env(_client(body=_EMPTY_LIVE), None),
        patch("database.cache.get_kv_cache", return_value=kv),
    ):
        res = await fetch_symbol_gex_metrics("NVDA")
    assert res == data
