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


def _patch_kv(read: Any = None) -> Any:
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


@pytest.mark.asyncio
async def test_profile_kv_hit_skips_api() -> None:
    p_read, p_save = _patch_kv({"name": "NVIDIA"})
    api = AsyncMock()
    with p_read, p_save as save, patch.object(
        f, "_execute_api_call", api
    ), patch.object(f, "_get_client", MagicMock()):
        assert (await f.get_company_profile("NVDA"))["name"] == "NVIDIA"
    api.assert_not_awaited()
    save.assert_not_awaited()


@pytest.mark.asyncio
async def test_profile_api_success_writes_kv() -> None:
    p_read, p_save = _patch_kv(None)
    api = AsyncMock(return_value={"name": "NVIDIA"})
    with p_read, p_save as save, patch.object(
        f, "_execute_api_call", api
    ), patch.object(f, "_get_client", MagicMock()):
        await f.get_company_profile("NVDA")
    save.assert_awaited_once_with("company_profile_NVDA", {"name": "NVIDIA"})
