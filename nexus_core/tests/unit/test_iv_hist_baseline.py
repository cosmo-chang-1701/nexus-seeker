"""iv_hist_mean_20d：近 20 個交易日（不含今日）DB 歷史 IV 均值。"""

from datetime import datetime
from unittest.mock import AsyncMock, MagicMock, patch

import pandas as pd
import pytest

from market_analysis.sentiment.iv_metrics import (
    _recent_iv_mean,
    fetch_and_calculate_iv_metrics,
)
from models.quant import IVMetrics


def _past_trading_days(n: int) -> list[str]:
    import market_time

    today = datetime.now(market_time.ny_tz).strftime("%Y-%m-%d")
    return [d for d in market_time.get_recent_trading_dates(n + 1) if d < today][-n:]


def _dates(n: int) -> dict[str, float]:
    return {f"2026-01-{i + 1:02d}": 0.30 + i * 0.01 for i in range(n)}


def test_mean_of_20_newest_when_given_30() -> None:
    d = _dates(30)
    newest = [d[k] for k in sorted(d)[-20:]]
    assert _recent_iv_mean(d) == pytest.approx(sum(newest) / 20)


def test_none_with_9_values() -> None:
    assert _recent_iv_mean(_dates(9)) is None


def test_mean_with_exactly_10() -> None:
    d = _dates(10)
    assert _recent_iv_mean(d) == pytest.approx(sum(d.values()) / 10)


def test_ignores_insertion_order() -> None:
    d = _dates(30)
    rev = dict(reversed(list(d.items())))
    assert _recent_iv_mean(rev) == pytest.approx(_recent_iv_mean(d))


def test_default_is_none() -> None:
    assert IVMetrics(symbol="X").iv_hist_mean_20d is None


def test_old_cached_dump_validates() -> None:
    dump = IVMetrics(symbol="X").model_dump()
    dump.pop("iv_hist_mean_20d")
    assert IVMetrics.model_validate(dump).iv_hist_mean_20d is None


@pytest.mark.asyncio
async def test_fetch_fills_iv_hist_mean_20d() -> None:
    days = _past_trading_days(40)
    ivs = [0.20 + 0.002 * i for i in range(40)]
    rows = list(reversed(list(zip(days, ivs))))
    expected = sum(ivs[-20:]) / 20

    conn = MagicMock()
    cursor = MagicMock()
    cursor.fetchall.return_value = rows
    conn.cursor.return_value = cursor
    chain = MagicMock(
        calls=pd.DataFrame({"strike": [91.5], "impliedVolatility": [0.3]}),
        puts=pd.DataFrame(),
    )

    with patch(
        "services.market_data_service.get_quote",
        new=AsyncMock(return_value={"c": 91.50, "h": 91.60, "l": 91.40}),
    ), patch(
        "market_analysis.sentiment.iv_metrics.is_market_open", return_value=True
    ), patch(
        "services.market_data_service.get_all_option_expiries",
        new=AsyncMock(return_value=["2099-01-16"]),
    ), patch(
        "services.market_data_service.get_option_chain",
        new=AsyncMock(return_value=chain),
    ), patch(
        "services.market_data_service.get_history_df",
        new=AsyncMock(return_value=pd.DataFrame()),
    ), patch("database.connection.get_read_connection", return_value=conn), patch(
        "market_analysis.sentiment.iv_metrics.save_historical_iv",
        new=AsyncMock(),
    ), patch(
        "market_analysis.sentiment.iv_metrics.get_last_stored_iv",
        return_value=None,
    ), patch("database.cache.save_kv_cache", new=AsyncMock()), patch(
        "database.calendar_cache.get_cached_earnings", return_value=None
    ), patch(
        "database.calendar_cache.get_macro_events_between", return_value=[]
    ), patch(
        "market_analysis.sentiment.iv_metrics._calculate_straddle_implied_em",
        new=AsyncMock(return_value=0.25),
    ), patch(
        "market_analysis.sentiment.iv_metrics._calculate_iv_term_structure",
        new=AsyncMock(return_value=("Contango", 0.95)),
    ):
        res = await fetch_and_calculate_iv_metrics("BIL", force_refresh=True)

    assert res.iv_source != "UNAVAILABLE"
    assert res.iv_hist_mean_20d == pytest.approx(expected)
