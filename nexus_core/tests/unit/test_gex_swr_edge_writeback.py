"""GEX SWR 降級時，較新的 edge 背景快照以真實資料時間寫回 kv。"""

import time
from contextlib import contextmanager, nullcontext
from typing import Any, Iterator, Optional
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from market_analysis.index_microstructure import fetch_symbol_gex_metrics

HOUR = 3600.0
OLD_KV_AGE = 10 * 86400.0
_MODULE = "market_analysis.index_microstructure"


def _snap(tag: float) -> dict[str, Any]:
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


def _edge(data: dict[str, Any], age_h: float) -> dict[str, Any]:
    return {"data": data, "age_seconds": age_h * HOUR}


def _kv(tag: float, age_s: float) -> dict[str, Any]:
    ts = time.time() - age_s
    return {"data": _snap(tag), "timestamp": ts, "data_timestamp": ts}


def _empty_live_client() -> MagicMock:
    resp = MagicMock()
    resp.status_code = 200
    resp.json.return_value = {
        "status": "success",
        "data": {"spot": 0, "gex_profile": {}, "expiry": None},
    }
    inst = AsyncMock()
    inst.get.return_value = resp
    inst.__aenter__.return_value = inst
    inst.__aexit__.return_value = None
    return MagicMock(return_value=inst)


@contextmanager
def _env(
    kv: dict[str, Any],
    edge: Optional[dict[str, Any]],
    save_exc: Optional[Exception] = None,
    mock_background: bool = True,
) -> Iterator[tuple[AsyncMock, MagicMock, dict[str, Any]]]:
    """有狀態的假 kv：save_kv_cache 寫入後，下一次 get_kv_cache 讀得到。
    mock_background=False 供 force_live 使用 (SingleFlight 需要真正的 task)。"""
    store: dict[str, Any] = {"gex_metrics_NVDA": kv}

    async def _save(key: str, value: Any) -> bool:
        if save_exc is not None:
            raise save_exc
        store[key] = value
        return True

    save = AsyncMock(side_effect=_save)
    create_task = MagicMock(side_effect=lambda coro: coro.close())
    with (
        patch("config.TUNNEL_URL", "http://fake-tunnel"),
        patch("httpx.AsyncClient", _empty_live_client()),
        patch(
            "services.edge_cache_client.get_cached_gex",
            new_callable=AsyncMock,
            return_value=edge,
        ),
        patch("database.cache.get_kv_cache", side_effect=store.get),
        patch("database.cache.save_kv_cache", save),
        patch(f"{_MODULE}._gex_swr_last_attempt", {}),
        patch("asyncio.create_task", create_task) if mock_background else nullcontext(),
    ):
        yield save, create_task, store


def _assert_stale(res: dict[str, Any], spot: float, age_s: float) -> None:
    assert res["spot"] == spot
    assert res["_is_stale_cache"] is True
    assert res["_data_ts"] == pytest.approx(time.time() - age_s, abs=5.0)


@pytest.mark.asyncio
async def test_a_newer_edge_written_back_with_real_data_time() -> None:
    edge_data = _snap(168.0)
    with _env(_kv(214.0, OLD_KV_AGE), _edge(edge_data, 10)) as (save, ct, _):
        res = await fetch_symbol_gex_metrics("NVDA")

    save.assert_awaited_once()
    key, envelope = save.await_args_list[0].args
    assert key == "gex_metrics_NVDA"
    assert envelope["data"] == _snap(168.0)
    assert "_is_stale_cache" not in envelope["data"]
    assert "_data_ts" not in envelope["data"]
    assert envelope["data_timestamp"] == pytest.approx(time.time() - 10 * HOUR, abs=5.0)
    assert envelope["timestamp"] == pytest.approx(time.time(), abs=5.0)
    _assert_stale(res, 168.0, 10 * HOUR)
    assert edge_data == _snap(168.0)
    ct.assert_called_once()


@pytest.mark.asyncio
async def test_a2_legacy_restamped_kv_replaced_by_edge() -> None:
    kv = _kv(214.0, 5 * 60)
    kv["data"]["_is_stale_cache"] = True
    with _env(kv, _edge(_snap(168.0), 10)) as (save, _, store):
        await fetch_symbol_gex_metrics("NVDA")

    save.assert_awaited_once()
    assert store["gex_metrics_NVDA"]["data"] == _snap(168.0)


@pytest.mark.asyncio
async def test_b_older_edge_not_written() -> None:
    with _env(_kv(214.0, 2 * HOUR), _edge(_snap(168.0), 10)) as (save, _, __):
        res = await fetch_symbol_gex_metrics("NVDA")

    save.assert_not_called()
    _assert_stale(res, 214.0, 2 * HOUR)


@pytest.mark.asyncio
async def test_c_sparse_edge_profile_not_written() -> None:
    sparse = _snap(168.0)
    sparse["gex_profile"] = {"168.0": 500.0}
    with _env(_kv(214.0, 2 * 86400), _edge(sparse, 10)) as (save, _, __):
        res = await fetch_symbol_gex_metrics("NVDA")

    save.assert_not_called()
    _assert_stale(res, 214.0, 2 * 86400)


@pytest.mark.asyncio
async def test_d_edge_older_than_cap_not_written() -> None:
    with _env(_kv(214.0, OLD_KV_AGE), _edge(_snap(168.0), 100)) as (save, _, __):
        res = await fetch_symbol_gex_metrics("NVDA")

    save.assert_not_called()
    assert res["gex_profile"] == {}


@pytest.mark.asyncio
async def test_e_force_live_does_not_write_back() -> None:
    with _env(
        _kv(214.0, OLD_KV_AGE), _edge(_snap(168.0), 10), mock_background=False
    ) as (save, _, __):
        res = await fetch_symbol_gex_metrics("NVDA", force_live=True)

    save.assert_not_called()
    _assert_stale(res, 168.0, 10 * HOUR)


@pytest.mark.asyncio
async def test_f_same_edge_snapshot_written_only_once() -> None:
    clock = [time.time()]
    with patch("time.time", side_effect=lambda: clock[0]):
        with _env(_kv(214.0, OLD_KV_AGE), _edge(_snap(168.0), 10)) as (
            save,
            _,
            store,
        ):
            first = await fetch_symbol_gex_metrics("NVDA")
            written_ts = store["gex_metrics_NVDA"]["data_timestamp"]
            # 固定的 age_seconds + 前進數秒的時鐘，模擬換算資料時間的秒級誤差。
            clock[0] += 7.0
            second = await fetch_symbol_gex_metrics("NVDA")

    save.assert_awaited_once()
    assert store["gex_metrics_NVDA"]["data_timestamp"] == written_ts
    # 寫回後 kv 仍超過 30 分鐘：不走新鮮捷徑，回傳值仍標記為降級備援。
    assert first["_is_stale_cache"] is True
    assert second["_is_stale_cache"] is True
    assert second["spot"] == 168.0


@pytest.mark.asyncio
async def test_g_write_failure_keeps_return_value() -> None:
    with _env(
        _kv(214.0, OLD_KV_AGE),
        _edge(_snap(168.0), 10),
        save_exc=RuntimeError("boom"),
    ) as (save, ct, _):
        res = await fetch_symbol_gex_metrics("NVDA")

    save.assert_awaited_once()
    ct.assert_called_once()
    _assert_stale(res, 168.0, 10 * HOUR)
