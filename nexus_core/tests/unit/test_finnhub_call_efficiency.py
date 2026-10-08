"""PR-B：Finnhub 呼叫效率（ETF 判斷、財報日曆記憶化、Profile 持久化）。"""

from datetime import datetime
from typing import Any, Iterator
from unittest.mock import AsyncMock, MagicMock, patch
from zoneinfo import ZoneInfo

import pytest

from services.market_data_service import fundamentals as f
from services.market_data_service.caches import (
    clear_earnings_calendar_cache,
    clear_etf_cache,
    clear_profile_cache,
)

_NY = ZoneInfo("America/New_York")


@pytest.fixture(autouse=True)
def _clean() -> Iterator[None]:
    clear_etf_cache()
    clear_profile_cache()
    clear_earnings_calendar_cache()
    yield
    clear_etf_cache()
    clear_profile_cache()
    clear_earnings_calendar_cache()


def _patch_kv(read: Any = None, age: float = 60.0) -> Any:
    """read 供 is_etf（get_kv_cache_fresh）；(read, age) 供 profile（get_kv_cache_with_age）。"""
    return (
        patch("database.cache.get_kv_cache_fresh", MagicMock(return_value=read)),
        patch("database.cache.save_kv_cache", AsyncMock(return_value=True)),
    )


@pytest.mark.asyncio
async def test_is_etf_etp_true_and_writes_kv() -> None:
    p_read, p_save = _patch_kv(None)
    api = AsyncMock(return_value={"result": [{"symbol": "SPY", "type": "ETP"}]})
    with p_read, p_save as save, patch.object(
        f, "_execute_api_call", api
    ), patch.object(f, "_get_client", MagicMock()):
        assert await f.is_etf("SPY") is True
    save.assert_awaited_once_with("etf_flag_SPY", True)


@pytest.mark.asyncio
async def test_is_etf_common_stock_false() -> None:
    p_read, p_save = _patch_kv(None)
    api = AsyncMock(
        return_value={"result": [{"symbol": "AAPL", "type": "Common Stock"}]}
    )
    with p_read, p_save as save_mock, patch.object(
        f, "_execute_api_call", api
    ), patch.object(f, "_get_client", MagicMock()):
        assert await f.is_etf("AAPL") is False
        # 非 ETF 不寫 kv，避免新上市 ETF 誤判後被鎖 30 天
        save_mock.assert_not_called()


@pytest.mark.asyncio
async def test_is_etf_kv_hit_skips_api() -> None:
    p_read, p_save = _patch_kv(True)
    api = AsyncMock()
    with p_read, p_save, patch.object(f, "_execute_api_call", api), patch.object(
        f, "_get_client", MagicMock()
    ):
        assert await f.is_etf("SPY") is True
    api.assert_not_awaited()


@pytest.mark.asyncio
async def test_is_etf_exception_negative_cached() -> None:
    p_read, p_save = _patch_kv(None)
    api = AsyncMock(side_effect=RuntimeError("boom"))
    with p_read, p_save, patch.object(f, "_execute_api_call", api), patch.object(
        f, "_get_client", MagicMock()
    ):
        assert await f.is_etf("SPY") is False
        assert await f.is_etf("SPY") is False
    assert api.await_count == 1


@pytest.mark.asyncio
async def test_earnings_calendar_same_day_memoized() -> None:
    api = AsyncMock(return_value={"earningsCalendar": [{"date": "2026-10-20"}]})
    with patch.object(f, "_execute_api_call", api), patch.object(
        f, "_get_client", MagicMock()
    ):
        a = await f.get_earnings_calendar("NVDA", "2026-10-08", "2026-12-01")
        a.append({"date": "x"})  # 回傳為拷貝，不得污染快取
        b = await f.get_earnings_calendar("nvda", "2026-10-08", "2026-12-01")
    assert api.await_count == 1
    assert b == [{"date": "2026-10-20"}]


@pytest.mark.asyncio
async def test_earnings_calendar_next_day_refetches() -> None:
    api = AsyncMock(return_value={"earningsCalendar": []})
    day1 = datetime(2026, 10, 8, 12, 0, tzinfo=_NY)
    day2 = datetime(2026, 10, 9, 0, 1, tzinfo=_NY)
    with patch.object(f, "_execute_api_call", api), patch.object(
        f, "_get_client", MagicMock()
    ), patch.object(f, "datetime") as dt:
        dt.now.return_value = day1
        await f.get_earnings_calendar("NVDA", "2026-10-08", "2026-12-01")
        dt.now.return_value = day2
        await f.get_earnings_calendar("NVDA", "2026-10-08", "2026-12-01")
    assert api.await_count == 2


@pytest.mark.asyncio
async def test_earnings_calendar_exception_not_cached() -> None:
    api = AsyncMock(side_effect=[RuntimeError("x"), {"earningsCalendar": []}])
    with patch.object(f, "_execute_api_call", api), patch.object(
        f, "_get_client", MagicMock()
    ):
        assert await f.get_earnings_calendar("NVDA", "2026-10-08", "2026-12-01") == []
        assert await f.get_earnings_calendar("NVDA", "2026-10-08", "2026-12-01") == []
    assert api.await_count == 2


def _patch_profile_kv(val: Any = None, age: Any = None) -> Any:
    return (
        patch(
            "database.cache.get_kv_cache_with_age", MagicMock(return_value=(val, age))
        ),
        patch("database.cache.save_kv_cache", AsyncMock(return_value=True)),
    )


@pytest.mark.asyncio
async def test_profile_kv_hit_skips_api() -> None:
    p_read, p_save = _patch_profile_kv({"name": "NVIDIA"}, 60.0)
    api = AsyncMock()
    with p_read, p_save as save, patch.object(
        f, "_execute_api_call", api
    ), patch.object(f, "_get_client", MagicMock()):
        assert (await f.get_company_profile("NVDA"))["name"] == "NVIDIA"
    api.assert_not_awaited()
    save.assert_not_awaited()


@pytest.mark.asyncio
async def test_profile_api_success_writes_kv() -> None:
    p_read, p_save = _patch_profile_kv(None, None)
    api = AsyncMock(return_value={"name": "NVIDIA"})
    with p_read, p_save as save, patch.object(
        f, "_execute_api_call", api
    ), patch.object(f, "_get_client", MagicMock()):
        await f.get_company_profile("NVDA")
    save.assert_awaited_once_with("company_profile_NVDA", {"name": "NVIDIA"})


# ---------------------------------------------------------------------------
# code review 修正項目
# ---------------------------------------------------------------------------
def _ny_now(hour: int = 12, day: int = 8) -> datetime:
    return datetime(2026, 10, day, hour, 0, tzinfo=_NY)


@pytest.mark.asyncio
async def test_earnings_calendar_recent_without_actual_short_ttl() -> None:
    """今天／昨天條目 epsActual 缺值 → 只快取 10 分鐘，之後重抓拿到實際值。"""
    first = {"earningsCalendar": [{"date": "2026-10-08", "epsActual": None}]}
    second = {"earningsCalendar": [{"date": "2026-10-08", "epsActual": 1.2}]}
    api = AsyncMock(side_effect=[first, second])
    with patch.object(f, "_execute_api_call", api), patch.object(
        f, "_get_client", MagicMock()
    ), patch.object(f, "datetime") as dt:
        dt.now.return_value = _ny_now(17)
        await f.get_earnings_calendar("NVDA", "2026-01-01", "2026-12-01")
        dt.now.return_value = _ny_now(17).replace(minute=5)  # 5 分鐘內命中
        await f.get_earnings_calendar("NVDA", "2026-01-01", "2026-12-01")
        assert api.await_count == 1
        dt.now.return_value = _ny_now(17).replace(minute=11)  # 逾 10 分鐘
        rows = await f.get_earnings_calendar("NVDA", "2026-01-01", "2026-12-01")
    assert api.await_count == 2
    assert rows[0]["epsActual"] == 1.2


@pytest.mark.asyncio
async def test_earnings_calendar_future_without_actual_cached_all_day() -> None:
    """未來條目 epsActual 本來就是 None，不應觸發短 TTL。"""
    api = AsyncMock(
        return_value={"earningsCalendar": [{"date": "2026-10-20", "epsActual": None}]}
    )
    with patch.object(f, "_execute_api_call", api), patch.object(
        f, "_get_client", MagicMock()
    ), patch.object(f, "datetime") as dt:
        dt.now.return_value = _ny_now(10)
        await f.get_earnings_calendar("NVDA", "2026-10-08", "2026-12-01")
        dt.now.return_value = _ny_now(20)
        await f.get_earnings_calendar("NVDA", "2026-10-08", "2026-12-01")
    assert api.await_count == 1


@pytest.mark.asyncio
async def test_earnings_calendar_empty_result_cached_one_hour_only() -> None:
    api = AsyncMock(return_value={"earningsCalendar": []})
    with patch.object(f, "_execute_api_call", api), patch.object(
        f, "_get_client", MagicMock()
    ), patch.object(f, "datetime") as dt:
        dt.now.return_value = _ny_now(10)
        await f.get_earnings_calendar("NVDA", "2026-10-08", "2026-12-01")
        dt.now.return_value = _ny_now(10).replace(minute=30)
        await f.get_earnings_calendar("NVDA", "2026-10-08", "2026-12-01")
        assert api.await_count == 1
        dt.now.return_value = _ny_now(11).replace(minute=1)
        await f.get_earnings_calendar("NVDA", "2026-10-08", "2026-12-01")
    assert api.await_count == 2


@pytest.mark.asyncio
async def test_earnings_calendar_falsy_data_treated_as_empty() -> None:
    api = AsyncMock(return_value=None)
    with patch.object(f, "_execute_api_call", api), patch.object(
        f, "_get_client", MagicMock()
    ):
        assert await f.get_earnings_calendar("NVDA", "2026-10-08", "2026-12-01") == []


@pytest.mark.asyncio
async def test_earnings_calendar_returned_dict_mutation_does_not_pollute_cache() -> (
    None
):
    api = AsyncMock(return_value={"earningsCalendar": [{"date": "2026-10-20"}]})
    with patch.object(f, "_execute_api_call", api), patch.object(
        f, "_get_client", MagicMock()
    ):
        a = await f.get_earnings_calendar("NVDA", "2026-10-08", "2026-12-01")
        a[0]["date"] = "polluted"
        b = await f.get_earnings_calendar("NVDA", "2026-10-08", "2026-12-01")
        b[0]["date"] = "polluted2"
        c = await f.get_earnings_calendar("NVDA", "2026-10-08", "2026-12-01")
    assert c == [{"date": "2026-10-20"}]


@pytest.mark.asyncio
async def test_earnings_calendar_concurrent_single_api_call() -> None:
    import asyncio

    async def slow(*_a: Any, **_k: Any) -> Any:
        await asyncio.sleep(0.05)
        return {"earningsCalendar": [{"date": "2026-10-20"}]}

    api = AsyncMock(side_effect=slow)
    with patch.object(f, "_execute_api_call", api), patch.object(
        f, "_get_client", MagicMock()
    ):
        results = await asyncio.gather(
            *[
                f.get_earnings_calendar("NVDA", "2026-10-08", "2026-12-01")
                for _ in range(5)
            ]
        )
    assert api.await_count == 1
    assert all(r == [{"date": "2026-10-20"}] for r in results)
    results[0][0]["date"] = "x"
    assert results[1][0]["date"] == "2026-10-20"


@pytest.mark.asyncio
async def test_is_etf_rate_limited_not_negative_cached() -> None:
    p_read, p_save = _patch_kv(None)
    api = AsyncMock(
        side_effect=[
            Exception("Finnhub rate limited, fast-circuit to fallback"),
            {"result": [{"symbol": "SPY", "type": "ETP"}]},
        ]
    )
    with p_read, p_save, patch.object(f, "_execute_api_call", api), patch.object(
        f, "_get_client", MagicMock()
    ), patch.object(f, "is_finnhub_rate_limited", MagicMock(return_value=True)):
        assert await f.is_etf("SPY") is False
        assert await f.is_etf("SPY") is True  # 未被負向快取，重新查詢
    assert api.await_count == 2


@pytest.mark.asyncio
async def test_is_etf_429_message_not_negative_cached() -> None:
    p_read, p_save = _patch_kv(None)
    api = AsyncMock(
        side_effect=[Exception("HTTP 429 Too Many Requests"), {"result": []}]
    )
    with p_read, p_save, patch.object(f, "_execute_api_call", api), patch.object(
        f, "_get_client", MagicMock()
    ), patch.object(f, "is_finnhub_rate_limited", MagicMock(return_value=False)):
        await f.is_etf("SPY")
        await f.is_etf("SPY")
    assert api.await_count == 2


@pytest.mark.asyncio
async def test_profile_kv_age_caps_memory_expiry() -> None:
    """kv 命中時記憶體到期 = now + (24h - kv 年齡)。"""
    from services.market_data_service.caches import _profile_cache

    age = 20 * 3600.0
    p_read, p_save = _patch_profile_kv({"name": "NVIDIA"}, age)
    with p_read, p_save, patch.object(f, "_get_client", MagicMock()):
        t0 = f.time.time()
        await f.get_company_profile("NVDA")
    _val, expiry = _profile_cache["NVDA"]
    assert 3.9 * 3600 < expiry - t0 < 4.1 * 3600


@pytest.mark.asyncio
async def test_profile_kv_older_than_24h_refetches() -> None:
    p_read, p_save = _patch_profile_kv({"name": "OLD"}, 25 * 3600.0)
    api = AsyncMock(return_value={"name": "NEW"})
    with p_read, p_save, patch.object(f, "_execute_api_call", api), patch.object(
        f, "_get_client", MagicMock()
    ):
        assert (await f.get_company_profile("NVDA"))["name"] == "NEW"
    api.assert_awaited_once()


@pytest.mark.asyncio
async def test_profile_empty_result_negative_cached_in_memory_only() -> None:
    p_read, p_save = _patch_profile_kv(None, None)
    api = AsyncMock(return_value={})
    with p_read as read, p_save as save, patch.object(
        f, "_execute_api_call", api
    ), patch.object(f, "_get_client", MagicMock()), patch(
        "database.cache.get_kv_cache_fresh", MagicMock(return_value=None)
    ):
        assert await f.get_company_profile("SPY") == {}
        assert await f.get_company_profile("SPY") == {}
    assert api.await_count == 1
    assert read.call_count == 1  # 第二次直接命中記憶體，不再讀 kv
    save.assert_not_awaited()


@pytest.mark.asyncio
async def test_profile_concurrent_single_api_call_and_copy() -> None:
    import asyncio

    async def slow(*_a: Any, **_k: Any) -> Any:
        await asyncio.sleep(0.05)
        return {"name": "NVIDIA"}

    p_read, p_save = _patch_profile_kv(None, None)
    api = AsyncMock(side_effect=slow)
    with p_read, p_save, patch.object(f, "_execute_api_call", api), patch.object(
        f, "_get_client", MagicMock()
    ):
        res = await asyncio.gather(*[f.get_company_profile("NVDA") for _ in range(5)])
        res[0]["name"] = "x"
        again = await f.get_company_profile("NVDA")
    assert api.await_count == 1
    assert again["name"] == "NVIDIA"


# ---------------------------------------------------------------------------
# 第二輪 review 修正
# ---------------------------------------------------------------------------
async def _gather_interactive_and_background(coro_factory: Any) -> list[Any]:
    """同時發出一個互動與一個背景呼叫（互動以 mark_interactive_request 標記）。"""
    import asyncio

    from services.market_data_service._core import mark_interactive_request

    async def _interactive() -> Any:
        with mark_interactive_request():
            return await coro_factory()

    return list(await asyncio.gather(_interactive(), coro_factory()))


def _slow_api(result: Any) -> AsyncMock:
    import asyncio

    async def slow(*_a: Any, **_k: Any) -> Any:
        await asyncio.sleep(0.05)
        return result

    return AsyncMock(side_effect=slow)


@pytest.mark.asyncio
async def test_profile_interactive_and_background_do_not_share_flight() -> None:
    p_read, p_save = _patch_profile_kv(None, None)
    api = _slow_api({"name": "NVIDIA"})
    with p_read, p_save, patch.object(f, "_execute_api_call", api), patch.object(
        f, "_get_client", MagicMock()
    ):
        await _gather_interactive_and_background(lambda: f.get_company_profile("NVDA"))
    assert api.await_count == 2


@pytest.mark.asyncio
async def test_earnings_calendar_interactive_and_background_do_not_share_flight() -> (
    None
):
    api = _slow_api({"earningsCalendar": [{"date": "2026-10-20"}]})
    with patch.object(f, "_execute_api_call", api), patch.object(
        f, "_get_client", MagicMock()
    ):
        await _gather_interactive_and_background(
            lambda: f.get_earnings_calendar("NVDA", "2026-10-08", "2026-12-01")
        )
    assert api.await_count == 2


@pytest.mark.asyncio
async def test_is_etf_concurrent_single_api_call() -> None:
    import asyncio

    p_read, p_save = _patch_kv(None)
    api = _slow_api({"result": [{"symbol": "SPY", "type": "ETP"}]})
    with p_read, p_save as save, patch.object(
        f, "_execute_api_call", api
    ), patch.object(f, "_get_client", MagicMock()):
        res = await asyncio.gather(*[f.is_etf("SPY") for _ in range(5)])
    assert res == [True] * 5
    assert api.await_count == 1
    save.assert_awaited_once_with("etf_flag_SPY", True)


@pytest.mark.asyncio
async def test_is_etf_interactive_and_background_do_not_share_flight() -> None:
    p_read, p_save = _patch_kv(None)
    api = _slow_api({"result": []})
    with p_read, p_save, patch.object(f, "_execute_api_call", api), patch.object(
        f, "_get_client", MagicMock()
    ):
        await _gather_interactive_and_background(lambda: f.is_etf("SPY"))
    assert api.await_count == 2


@pytest.mark.asyncio
async def test_profile_concurrent_reads_kv_only_once() -> None:
    import asyncio

    p_read, p_save = _patch_profile_kv({"name": "NVIDIA"}, 60.0)
    with p_read as read, p_save, patch.object(f, "_get_client", MagicMock()):
        await asyncio.gather(*[f.get_company_profile("NVDA") for _ in range(5)])
    assert read.call_count == 1


@pytest.mark.asyncio
async def test_profile_empty_known_etf_memory_negative_cached_24h() -> None:
    from services.market_data_service.caches import _etf_cache, _profile_cache

    _etf_cache["SPY"] = (True, f.time.time() + 3600)
    p_read, p_save = _patch_profile_kv(None, None)
    api = AsyncMock(return_value={})
    with p_read, p_save, patch.object(f, "_execute_api_call", api), patch.object(
        f, "_get_client", MagicMock()
    ):
        t0 = f.time.time()
        await f.get_company_profile("SPY")
    api.assert_awaited_once()  # 只打 company_profile2，不為判斷 ETF 額外打 API
    assert 23.9 * 3600 < _profile_cache["SPY"][1] - t0 < 24.1 * 3600


@pytest.mark.asyncio
async def test_profile_empty_known_etf_via_kv_flag_24h() -> None:
    from services.market_data_service.caches import _profile_cache

    p_read, p_save = _patch_profile_kv(None, None)
    api = AsyncMock(return_value={})
    with p_read, p_save, patch.object(f, "_execute_api_call", api), patch.object(
        f, "_get_client", MagicMock()
    ), patch("database.cache.get_kv_cache_fresh", MagicMock(return_value=True)):
        t0 = f.time.time()
        await f.get_company_profile("SPY")
    assert 23.9 * 3600 < _profile_cache["SPY"][1] - t0 < 24.1 * 3600


@pytest.mark.asyncio
async def test_profile_empty_unknown_symbol_negative_cached_1h() -> None:
    from services.market_data_service.caches import _profile_cache

    p_read, p_save = _patch_profile_kv(None, None)
    api = AsyncMock(return_value={})
    with p_read, p_save, patch.object(f, "_execute_api_call", api), patch.object(
        f, "_get_client", MagicMock()
    ), patch("database.cache.get_kv_cache_fresh", MagicMock(return_value=None)):
        t0 = f.time.time()
        await f.get_company_profile("NEWCO")
    assert 0.9 * 3600 < _profile_cache["NEWCO"][1] - t0 < 1.1 * 3600


def test_earnings_expiry_monday_reads_friday_after_close_pending() -> None:
    """週一讀到週五盤後條目（epsActual 缺）→ 10 分鐘（舊邏輯只認今天／昨天）。"""
    now_ny = datetime(2026, 10, 12, 9, 0, tzinfo=_NY)  # 週一
    rows = [{"date": "2026-10-09", "epsActual": None}]  # 週五
    expiry = f._earnings_cache_expiry(rows, now_ny)
    assert expiry == pytest.approx(now_ny.timestamp() + 600)


def test_earnings_expiry_old_and_future_entries_not_short() -> None:
    now_ny = datetime(2026, 10, 12, 9, 0, tzinfo=_NY)
    day_end = now_ny.replace(hour=23, minute=59, second=59).timestamp()
    for d in ("2026-10-08", "2026-10-20"):  # 已過期很久／未來
        assert f._earnings_cache_expiry(
            [{"date": d, "epsActual": None}], now_ny
        ) == pytest.approx(day_end)


def test_is_finnhub_rate_limit_error_matches() -> None:
    from services.market_data_service._core import is_finnhub_rate_limit_error

    for msg in (
        "HTTP 429 Too Many Requests",
        "API limit reached. Please try again later.",
        "too many requests",
        "Finnhub rate limited, fast-circuit to fallback",
    ):
        assert is_finnhub_rate_limit_error(Exception(msg)) is True
    assert is_finnhub_rate_limit_error(RuntimeError("boom")) is False
    assert is_finnhub_rate_limit_error(Exception("connection timeout")) is False


@pytest.mark.asyncio
async def test_earnings_calendar_cache_hit_refreshes_lru_order() -> None:
    from services.market_data_service.caches import _earnings_calendar_cache

    api = AsyncMock(return_value={"earningsCalendar": [{"date": "2026-10-20"}]})
    with patch.object(f, "_execute_api_call", api), patch.object(
        f, "_get_client", MagicMock()
    ):
        await f.get_earnings_calendar("AAA", "2026-10-08", "2026-12-01")
        await f.get_earnings_calendar("BBB", "2026-10-08", "2026-12-01")
        await f.get_earnings_calendar("AAA", "2026-10-08", "2026-12-01")  # 命中
    assert list(_earnings_calendar_cache)[-1][0] == "AAA"
