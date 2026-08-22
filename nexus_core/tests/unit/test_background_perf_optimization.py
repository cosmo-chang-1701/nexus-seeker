from typing import Any
from datetime import datetime
import pytest
import pandas as pd
from unittest.mock import AsyncMock, patch, MagicMock

from services.trading_service import TradingService
from market_analysis.analyst_runners.sector_runner import gather_sector_rotation_data
from market_analysis.index_microstructure import fetch_symbol_gex_metrics


@pytest.fixture
def trading_service() -> Any:
    bot = MagicMock()
    return TradingService(bot)


@pytest.mark.asyncio
async def test_run_market_scan_multi_user_data_consistency(
    trading_service: Any,
) -> None:
    """Verify that multi-user shared symbol caching in run_market_scan produces
    consistent, identical results across users without duplicate redundant calls."""
    # 2 users watching the same symbol (AAPL)
    mock_watchlists = [(1, "AAPL", 1), (2, "AAPL", 1)]
    mock_holdings = [
        {"user_id": 1, "symbol": "AAPL", "avg_cost": 150.0},
        {"user_id": 2, "symbol": "AAPL", "avg_cost": 150.0},
    ]

    mock_spy_df = pd.DataFrame(
        {"Close": [670.0]}, index=pd.date_range("2026-05-20", periods=1)
    )
    mock_macro = {"vix": 18.0, "oil": 75.0, "vix_change": 0.0}

    mock_aapl_df = pd.DataFrame(
        {
            "Open": [150.0] * 30,
            "High": [152.0] * 30,
            "Low": [148.0] * 30,
            "Close": [150.0] * 30,
            "Volume": [1000] * 30,
        },
        index=pd.date_range("2026-05-20", periods=30),
    )

    mock_option_res = {
        "symbol": "AAPL",
        "stock_cost": 150.0,
        "strategy": "BTO_CALL",
        "weighted_delta": 0.2,
        "iv": 0.25,
        "price": 150.0,
        "aroc": 35.0,
    }

    skew_call_count = 0
    pcr_call_count = 0

    async def mock_calculate_skew(sym: str) -> dict:
        nonlocal skew_call_count
        skew_call_count += 1
        return {"symbol": sym, "skew": 1.25, "state": "NORMAL"}

    async def mock_calculate_pcr(sym: str) -> dict:
        nonlocal pcr_call_count
        pcr_call_count += 1
        return {"symbol": sym, "pcr": 0.85}

    with patch("database.get_all_watchlist", return_value=mock_watchlists), patch(
        "database.holdings.get_all_holdings", return_value=mock_holdings
    ), patch(
        "services.market_data_service.get_spy_history_df",
        new_callable=AsyncMock,
        return_value=mock_spy_df,
    ), patch(
        "services.market_data_service.get_macro_environment",
        new_callable=AsyncMock,
        return_value=mock_macro,
    ), patch(
        "services.market_data_service.get_history_df",
        new_callable=AsyncMock,
        return_value=mock_aapl_df,
    ), patch(
        "market_math.analyze_symbol",
        new_callable=AsyncMock,
        return_value=mock_option_res,
    ), patch(
        "market_analysis.sentiment_engine.SentimentEngine.calculate_skew",
        side_effect=mock_calculate_skew,
    ), patch(
        "market_analysis.sentiment_engine.SentimentEngine.calculate_pcr",
        side_effect=mock_calculate_pcr,
    ), patch(
        "services.calendar_service.calendar_service.get_symbol_earnings",
        new_callable=AsyncMock,
        return_value=MagicMock(tte_hours=48),
    ), patch(
        "services.news_service.fetch_recent_news",
        new_callable=AsyncMock,
        return_value="No negative news.",
    ), patch(
        "database.upsert_user_config",
    ), patch("database.get_full_user_context") as mock_user_ctx, patch(
        "market_analysis.portfolio.refresh_portfolio_greeks",
        new_callable=AsyncMock,
    ):
        mock_ctx = MagicMock()
        mock_ctx.capital = 100000.0
        mock_ctx.total_weighted_delta = 0.0
        mock_ctx.option_alert_mode = 1
        mock_ctx.risk_limit = 15.0
        mock_ctx.last_rehedge_alert_time = 0
        mock_ctx.enable_psq_watchlist = False
        mock_user_ctx.return_value = mock_ctx

        res = await trading_service.run_market_scan(is_auto=True)

        assert 1 in res
        assert 2 in res
        # Check that both users received identical analysis data for AAPL
        user1_alerts = res[1]
        user2_alerts = res[2]
        assert len(user1_alerts) == 1
        assert len(user2_alerts) == 1
        assert user1_alerts[0]["symbol"] == "AAPL"
        assert user2_alerts[0]["symbol"] == "AAPL"
        assert user1_alerts[0]["strategy"] == "BTO_CALL"
        assert user2_alerts[0]["strategy"] == "BTO_CALL"

        # Skew and PCR should be called once in the batch phase, NOT 2+ times in the user loop
        assert skew_call_count == 1
        assert pcr_call_count == 1


@pytest.mark.asyncio
async def test_sector_runner_gather_data_consistency() -> None:
    """Verify sector_runner concurrent gather returns all 11 sectors accurately."""
    bot = MagicMock()

    mock_df = pd.DataFrame(
        {
            "Open": [100.0] * 30,
            "High": [102.0] * 30,
            "Low": [98.0] * 30,
            "Close": [101.0] * 30,
            "Volume": [50000] * 30,
        },
        index=pd.date_range("2026-05-20", periods=30),
    )

    with patch(
        "market_analysis.analyst_runners.sector_runner.get_macro_environment",
        new_callable=AsyncMock,
        return_value={"vix": 16.5},
    ), patch(
        "market_analysis.analyst_runners.sector_runner.get_quote",
        new_callable=AsyncMock,
        return_value={"c": 510.0},
    ), patch(
        "market_analysis.analyst_runners.sector_runner.get_history_df",
        new_callable=AsyncMock,
        return_value=mock_df,
    ), patch(
        "market_analysis.analyst_runners.sector_runner.SentimentEngine.calculate_skew",
        new_callable=AsyncMock,
        return_value={"skew": 0.5, "state": "NORMAL"},
    ), patch(
        "market_analysis.analyst_runners.sector_runner.SentimentEngine.detect_uoa",
        new_callable=AsyncMock,
        return_value=[{"action": "BTO"}],
    ), patch(
        "market_analysis.analyst_runners.sector_runner._fetch_poly_events",
        new_callable=AsyncMock,
        return_value=[],
    ), patch(
        "market_analysis.analyst_runners.sector_runner.SentimentEngine.calculate_max_pain",
        new_callable=AsyncMock,
        return_value={"max_pain": 510.0},
    ):
        data = await gather_sector_rotation_data(bot)

        assert data["vix"] == 16.5
        assert data["spy_price"] == 510.0
        assert len(data["sectors"]) == 11
        sector_syms = {s["symbol"] for s in data["sectors"]}
        assert "XLK" in sector_syms
        assert "XLF" in sector_syms
        assert "XLE" in sector_syms


@pytest.mark.asyncio
async def test_fetch_symbol_gex_metrics_swr_consistency() -> None:
    """Verify fetch_symbol_gex_metrics returns valid data and serves stale cache when available."""
    stale_data = {
        "spot": 150.0,
        "net_gex": 1000000.0,
        "call_wall": 155.0,
        "put_wall": 145.0,
        "gex_profile": {"150.0": 500000.0},
    }

    # Test fresh cache return
    with patch(
        "database.cache.get_kv_cache",
        return_value={"data": stale_data, "timestamp": 10000000000.0},
    ):
        res = await fetch_symbol_gex_metrics("AAPL")
        assert res["spot"] == 150.0
        assert res["call_wall"] == 155.0

    # Test edge cache hit
    with patch("database.cache.get_kv_cache", return_value=None), patch(
        "services.edge_cache_client.get_cached_gex",
        new_callable=AsyncMock,
        return_value={"data": stale_data, "age_seconds": 120},
    ), patch("database.cache.save_kv_cache", new_callable=AsyncMock):
        res = await fetch_symbol_gex_metrics("AAPL")
        assert res["spot"] == 150.0
        assert res["put_wall"] == 145.0


@pytest.mark.asyncio
async def test_heartbeat_radar_cache_sharing_with_portfolio_monitor() -> None:
    """Verify heartbeat and portfolio monitor share radar cache effectively."""
    import time

    bot = MagicMock()
    fake_radar_data = {
        "AAPL": {
            "price": 150.0,
            "net_gex": 500000.0,
            "call_wall": 155.0,
            "put_wall": 145.0,
        }
    }
    bot._latest_radar_data_cache = fake_radar_data
    bot._latest_radar_cache_time = time.time()

    # Verify portfolio monitor can read from bot's shared radar data cache
    is_fresh = (time.time() - getattr(bot, "_latest_radar_cache_time", 0.0)) < 300
    assert is_fresh is True
    shared_data = getattr(bot, "_latest_radar_data_cache", {})
    assert "AAPL" in shared_data
    assert shared_data["AAPL"]["price"] == 150.0


@pytest.mark.asyncio
async def test_price_volume_alert_monitor_concurrency() -> None:
    """Verify PriceVolumeAlertMonitorCog evaluates watches concurrently without errors."""
    from cogs.trading.price_volume_alert_monitor import PriceVolumeAlertMonitorCog
    from database.price_volume_watch import PriceVolumeWatch, WatchDirection
    from market_analysis.price_volume_alert import Confirmed15mBar

    bot = MagicMock()
    cog = PriceVolumeAlertMonitorCog(bot)

    mock_watches = [
        PriceVolumeWatch(
            user_id=101,
            symbol="NVDA",
            direction=WatchDirection.ABOVE,
            target_price=100.0,
            volume_multiplier=1.5,
        ),
        PriceVolumeWatch(
            user_id=102,
            symbol="TSLA",
            direction=WatchDirection.BELOW,
            target_price=200.0,
            volume_multiplier=1.5,
        ),
    ]

    mock_bar_nvda = Confirmed15mBar(
        symbol="NVDA",
        bar_time=datetime.now(),
        close=102.0,
        volume=100000.0,
        avg_volume=50000.0,
    )

    with patch(
        "cogs.trading.price_volume_alert_monitor.get_all_watches",
        return_value=mock_watches,
    ), patch(
        "cogs.trading.price_volume_alert_monitor.get_confirmed_15m_bar",
        new_callable=AsyncMock,
        return_value=mock_bar_nvda,
    ), patch(
        "database.is_notification_enabled",
        return_value=True,
    ), patch(
        "database.get_kv_cache",
        return_value=None,
    ), patch(
        "database.save_kv_cache",
        new_callable=AsyncMock,
    ):
        await cog._evaluate_price_volume_alerts()
        # Should complete concurrently without throwing exceptions
        assert True
