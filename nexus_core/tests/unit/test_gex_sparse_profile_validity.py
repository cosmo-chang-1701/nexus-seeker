"""GEX profile 有效性：近端履約價過少（夜間 OI 未就緒）視為無效並降級。"""

import time
from contextlib import contextmanager
from typing import Any, Iterator, Optional
from unittest.mock import AsyncMock, MagicMock, patch

from market_analysis.index_microstructure import (
    _has_valid_gex_profile,
    _scrape_symbol_gex_raw,
    fetch_symbol_gex_metrics,
)

_DEGENERATE: dict = {
    "spot": 166.19,
    "net_gex": 1774.69,
    "call_wall": 335.0,
    "put_wall": 0.0,
    "gex_profile": {"335.0": 1774.69},
}

_FULL: dict = {
    "spot": 166.19,
    "net_gex": 5000.0,
    "call_wall": 170.0,
    "put_wall": 160.0,
    "gex_profile": {"160.0": -300.0, "165.0": 400.0, "170.0": 900.0},
}


def test_nvda_overnight_single_far_strike_invalid() -> None:
    assert not _has_valid_gex_profile(
        {"spot": 166.19, "gex_profile": {"335.0": 1774.69}}
    )


def test_three_near_strikes_valid() -> None:
    assert _has_valid_gex_profile(_FULL)


def test_two_near_many_far_invalid() -> None:
    prof = {"160": 1.0, "170": 2.0, "300": 3.0, "320": 4.0, "50": 5.0}
    assert not _has_valid_gex_profile({"spot": 166.0, "gex_profile": prof})


def test_missing_spot_requires_three_nonzero() -> None:
    assert not _has_valid_gex_profile({"gex_profile": {"1": 5.0, "2": 5.0, "3": 0.0}})
    assert _has_valid_gex_profile({"gex_profile": {"1": 5.0, "200": 5.0, "900": 1.0}})
    assert _has_valid_gex_profile(
        {"spot": "bad", "gex_profile": {"1": 5.0, "200": 5.0, "900": 1.0}}
    )
    assert not _has_valid_gex_profile(
        {"spot": -1, "gex_profile": {"1": 5.0, "200": 5.0}}
    )


def test_non_numeric_keys_ignored() -> None:
    prof: dict[Any, float] = {
        "abc": 9.0,
        "nan": 9.0,
        "160": 1.0,
        165.0: 1.0,
        "170": 1.0,
    }
    assert _has_valid_gex_profile({"spot": 166.0, "gex_profile": prof})
    prof2: dict[Any, float] = {"abc": 9.0, "x": 9.0, "160": 1.0, "165": 1.0}
    assert not _has_valid_gex_profile({"spot": 166.0, "gex_profile": prof2})


def _client(body: dict) -> MagicMock:
    resp = MagicMock()
    resp.status_code = 200
    resp.json.return_value = body
    inst = AsyncMock()
    inst.get.return_value = resp
    inst.__aenter__.return_value = inst
    inst.__aexit__.return_value = None
    return MagicMock(return_value=inst)


@contextmanager
def _env(
    live: dict, edge: Optional[dict] = None, kv: Optional[dict] = None
) -> Iterator[AsyncMock]:
    save = AsyncMock()
    with (
        patch("config.TUNNEL_URL", "http://fake-tunnel"),
        patch("httpx.AsyncClient", _client({"status": "success", "data": live})),
        patch(
            "services.edge_cache_client.get_cached_gex",
            new_callable=AsyncMock,
            return_value=edge,
        ),
        patch("database.cache.get_kv_cache", return_value=kv),
        patch("database.cache.save_kv_cache", save),
    ):
        yield save


async def test_live_degenerate_falls_back_to_edge_snapshot_without_kv_write() -> None:
    edge = {"data": dict(_FULL), "age_seconds": 9 * 3600.0}
    with _env(_DEGENERATE, edge=edge) as save:
        res = await _scrape_symbol_gex_raw("NVDA", None, 0.0, edge)
    assert res["call_wall"] == 170.0
    assert res["_is_stale_cache"] is True
    save.assert_not_called()


async def test_degenerate_fresh_kv_does_not_short_circuit() -> None:
    now = time.time()
    kv = {"data": dict(_DEGENERATE), "timestamp": now, "data_timestamp": now}
    edge = {"data": dict(_FULL), "age_seconds": 9 * 3600.0}
    with _env(_DEGENERATE, edge=edge, kv=kv) as save:
        res = await fetch_symbol_gex_metrics("NVDA", force_live=False)
    assert res["call_wall"] == 170.0
    assert res["_is_stale_cache"] is True
    save.assert_not_called()


async def test_degenerate_fresh_edge_snapshot_not_written_to_kv() -> None:
    edge = {"data": dict(_DEGENERATE), "age_seconds": 60.0}
    with _env(_DEGENERATE, edge=edge, kv=None) as save:
        res = await fetch_symbol_gex_metrics("NVDA", force_live=False)
    save.assert_not_called()
    assert res["gex_profile"] == {}
