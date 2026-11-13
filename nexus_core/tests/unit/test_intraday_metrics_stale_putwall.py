"""單元測試：過期（last-known-good）GEX PutWall 不得以新時間戳寫回 kv 快取。"""

from __future__ import annotations

from typing import Any, Iterator
from unittest.mock import AsyncMock, patch

import pandas as pd
import pytest

from market_analysis.intraday_pipeline.metrics import (
    _WATCHLIST_METRICS_CACHE,
    build_enhanced_watchlist_metrics,
)
from market_analysis.sentiment_engine import SentimentEngine


@pytest.fixture(autouse=True)
def _clear_cache() -> Iterator[None]:
    _WATCHLIST_METRICS_CACHE.clear()
    yield
    _WATCHLIST_METRICS_CACHE.clear()


def _df(base: float, step: float) -> pd.DataFrame:
    dates = pd.date_range("2025-01-01", periods=90, freq="D")
    return pd.DataFrame(
        {
            "Open": [base + i * step for i in range(90)],
            "High": [base + 1.5 + i * step for i in range(90)],
            "Low": [base - 1.0 + i * step for i in range(90)],
            "Close": [base + 0.5 + i * step for i in range(90)],
            "Volume": [1_000_000 + i * 1000 for i in range(90)],
        },
        index=dates,
    )


async def _run(gex: dict[str, Any]) -> tuple[Any, AsyncMock]:
    save_mock = AsyncMock()
    with patch(
        "services.market_data_service.get_quote",
        new_callable=AsyncMock,
        return_value={"c": 172.5},
    ), patch(
        "services.market_data_service.get_history_df",
        new_callable=AsyncMock,
        return_value=_df(100.0, 0.8),
    ), patch(
        "services.market_data_service.get_spy_history_df",
        new_callable=AsyncMock,
        return_value=_df(400.0, 0.4),
    ), patch(
        "services.market_data_service.get_basic_financials",
        new_callable=AsyncMock,
        return_value={"peTTM": 31.2},
    ), patch(
        "services.market_data_service.get_company_profile",
        new_callable=AsyncMock,
        return_value={"exchange": "NASDAQ"},
    ), patch.object(
        SentimentEngine,
        "fetch_and_calculate_iv_metrics",
        new_callable=AsyncMock,
        return_value=None,
    ), patch.object(
        SentimentEngine,
        "calculate_skew",
        new_callable=AsyncMock,
        return_value=None,
    ), patch.object(
        SentimentEngine,
        "calculate_pcr",
        new_callable=AsyncMock,
        return_value=None,
    ), patch(
        "market_analysis.index_microstructure.fetch_symbol_gex_metrics",
        new_callable=AsyncMock,
        return_value=gex,
    ), patch("market_analysis.risk_engine.calculate_beta", return_value=1.0), patch(
        "market_analysis.intraday_pipeline.metrics.save_cached_gex_putwall",
        save_mock,
    ), patch(
        "market_analysis.intraday_pipeline.metrics.save_cached_volume_poc",
        new_callable=AsyncMock,
    ), patch(
        "database.squeeze_cache.get_squeeze_cache",
        return_value={
            "is_expired": False,
            "is_squeezing": False,
            "momentum": 0.0,
            "direction": "⚪",
        },
    ), patch("database.squeeze_cache.save_squeeze_cache", new_callable=AsyncMock):
        metrics = await build_enhanced_watchlist_metrics("MSFT")
    return metrics, save_mock


@pytest.mark.asyncio
async def test_stale_gex_putwall_not_saved() -> None:
    metrics, save_mock = await _run(
        {"put_wall": 165.0, "call_wall": 180.0, "_is_stale_cache": True}
    )
    assert metrics is not None
    assert metrics.gex_max_put_wall == 165.0
    save_mock.assert_not_awaited()


@pytest.mark.asyncio
async def test_fresh_gex_putwall_saved() -> None:
    metrics, save_mock = await _run({"put_wall": 165.0, "call_wall": 180.0})
    assert metrics is not None
    assert metrics.gex_max_put_wall == 165.0
    save_mock.assert_awaited_once_with("MSFT", 165.0)
