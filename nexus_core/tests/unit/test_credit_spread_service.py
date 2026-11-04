"""信用利差服務：摘要數學、快取命中、過期刷新、失敗退避與逾時（全部 mock，無網路）。"""

import asyncio
from datetime import date
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest

import services.credit_spread_service as svc
from market_analysis.macro_signals import Observation


def _obs(d: date, v: float) -> Observation:
    return Observation(d, v, d)


@pytest.fixture(autouse=True)
def _reset_backoff() -> None:
    svc._last_failure_at = None


def test_summarize_empty_returns_none() -> None:
    assert svc.summarize_oas([]) is None


def test_summarize_lookback_picks_latest_obs_on_or_before_30_days() -> None:
    obs = [
        _obs(date(2026, 9, 30), 3.00),
        _obs(date(2026, 10, 2), 3.10),  # 11/02 - 30d = 10/03，此筆為 ≤ 的最新一筆
        _obs(date(2026, 10, 20), 3.30),
        _obs(date(2026, 11, 2), 3.43),
    ]
    out = svc.summarize_oas(obs)
    assert out == {"bps": 343, "change_1m_bps": 33, "obs_date": "2026-11-02"}


def test_summarize_lookback_exact_boundary_and_unsorted() -> None:
    obs = [_obs(date(2026, 11, 2), 0.94), _obs(date(2026, 10, 3), 0.89)]
    out = svc.summarize_oas(obs)
    assert out is not None and out["change_1m_bps"] == 5


def test_summarize_lookback_no_reference_gives_none_change() -> None:
    out = svc.summarize_oas(
        [_obs(date(2026, 10, 20), 3.0), _obs(date(2026, 11, 2), 3.2)]
    )
    assert out == {"bps": 320, "change_1m_bps": None, "obs_date": "2026-11-02"}


_FRESH: dict[str, Any] = {"hy": {"bps": 343}, "ig": {"bps": 94}}


async def test_cache_hit_makes_no_fetch() -> None:
    with (
        patch.object(svc, "get_kv_cache_with_age", return_value=(_FRESH, 60.0)),
        patch.object(svc, "fetch_fred_series", new=AsyncMock()) as fetch,
    ):
        assert await svc.get_credit_spreads() == _FRESH
    fetch.assert_not_called()


async def test_stale_cache_triggers_fetch_and_saves() -> None:
    obs = [_obs(date(2026, 10, 1), 3.0), _obs(date(2026, 11, 2), 3.43)]
    save = AsyncMock(return_value=True)
    with (
        patch.object(svc, "get_kv_cache_with_age", return_value=(_FRESH, 5 * 3600.0)),
        patch.object(
            svc, "fetch_fred_series", new=AsyncMock(return_value=obs)
        ) as fetch,
        patch.object(svc, "save_kv_cache", new=save),
    ):
        out = await svc.get_credit_spreads()
    assert fetch.await_count == 2
    assert all(c.kwargs["kind"] == "daily" for c in fetch.await_args_list)
    assert out["hy"]["bps"] == 343 and out["ig"]["bps"] == 343
    save.assert_awaited_once()


async def test_fetch_failure_returns_old_value_and_backoff() -> None:
    save = AsyncMock()
    boom = AsyncMock(side_effect=RuntimeError("boom"))
    with (
        patch.object(svc, "get_kv_cache_with_age", return_value=(_FRESH, 9 * 3600.0)),
        patch.object(svc, "fetch_fred_series", new=boom),
        patch.object(svc, "save_kv_cache", new=save),
    ):
        assert await svc.get_credit_spreads() == _FRESH
        calls = boom.await_count
        assert calls >= 1 and svc._last_failure_at is not None
        # 退避窗口內不再打 FRED
        assert await svc.get_credit_spreads() == _FRESH
        assert boom.await_count == calls
    save.assert_not_called()


async def test_backoff_expires_then_refetches() -> None:
    svc._last_failure_at = -10_000.0
    obs = [_obs(date(2026, 11, 2), 1.0)]
    with (
        patch.object(svc, "get_kv_cache_with_age", return_value=(None, None)),
        patch.object(
            svc, "fetch_fred_series", new=AsyncMock(return_value=obs)
        ) as fetch,
        patch.object(svc, "save_kv_cache", new=AsyncMock()),
    ):
        out = await svc.get_credit_spreads()
    assert fetch.await_count == 2 and out["hy"]["bps"] == 100


async def test_no_cache_and_failure_returns_empty_sides() -> None:
    with (
        patch.object(svc, "get_kv_cache_with_age", return_value=(None, None)),
        patch.object(
            svc, "fetch_fred_series", new=AsyncMock(side_effect=ValueError("x"))
        ),
    ):
        assert await svc.get_credit_spreads() == {"hy": None, "ig": None}


async def test_timeout_returns_old_value_and_enters_backoff() -> None:
    async def _slow(*_a: Any, **_k: Any) -> list[Observation]:
        await asyncio.sleep(5)
        return []

    with (
        patch.object(svc, "get_kv_cache_with_age", return_value=(_FRESH, 9 * 3600.0)),
        patch.object(svc, "fetch_fred_series", new=_slow),
        patch.object(svc, "_FETCH_TIMEOUT_SECONDS", 0.01),
    ):
        assert await svc.get_credit_spreads() == _FRESH
    assert svc._last_failure_at is not None
