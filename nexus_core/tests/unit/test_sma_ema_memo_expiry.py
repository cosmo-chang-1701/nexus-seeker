"""單元測試：SMA/EMA 備忘錄到期不得晚於底層日線快取（盤中部分值不跨收盤）。"""

from __future__ import annotations

from datetime import datetime
from typing import Iterator
from unittest.mock import AsyncMock, patch

import pandas as pd
import pytest

from market_time import ny_tz
from services.market_data_service import history
from services.market_data_service.caches import (
    _EMA_CACHE_TTL,
    _SMA_CACHE_TTL,
    _ema_cache,
    _sma_cache,
)

# 2026-11-12 為週四（一般交易日），收盤 16:00 ET
_CLOSE = datetime(2026, 11, 12, 16, 0, tzinfo=ny_tz).timestamp()
_INTRADAY = datetime(2026, 11, 12, 15, 30, tzinfo=ny_tz).timestamp()
_OFFHOURS = datetime(2026, 11, 12, 22, 0, tzinfo=ny_tz).timestamp()


@pytest.fixture(autouse=True)
def _clear() -> Iterator[None]:
    _sma_cache.clear()
    _ema_cache.clear()
    yield
    _sma_cache.clear()
    _ema_cache.clear()


def _df() -> pd.DataFrame:
    idx = pd.date_range("2025-01-01", periods=300, freq="D")
    return pd.DataFrame({"Close": [100.0 + i * 0.1 for i in range(300)]}, index=idx)


async def _sma_expiry(now: float) -> float:
    with patch.object(history.time, "time", return_value=now), patch.object(
        history, "get_history_df", new_callable=AsyncMock, return_value=_df()
    ):
        assert await history.get_sma("TEST", 50) is not None
    return float(_sma_cache[("TEST", 50)][1])


async def _ema_expiry(now: float) -> float:
    with patch.object(history.time, "time", return_value=now), patch.object(
        history, "get_history_df", new_callable=AsyncMock, return_value=_df()
    ):
        assert await history.get_ema("TEST", 21) is not None
    return float(_ema_cache[("TEST", 21)][1])


@pytest.mark.asyncio
async def test_sma_memo_capped_at_close_intraday() -> None:
    assert await _sma_expiry(_INTRADAY) <= _CLOSE + 60


@pytest.mark.asyncio
async def test_sma_memo_offhours_ttl() -> None:
    assert await _sma_expiry(_OFFHOURS) <= _OFFHOURS + _SMA_CACHE_TTL


@pytest.mark.asyncio
async def test_ema_memo_capped_at_close_intraday() -> None:
    assert await _ema_expiry(_INTRADAY) <= _CLOSE + 60


@pytest.mark.asyncio
async def test_ema_memo_offhours_ttl() -> None:
    assert await _ema_expiry(_OFFHOURS) <= _OFFHOURS + _EMA_CACHE_TTL
