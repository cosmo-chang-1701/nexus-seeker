import pytest
from unittest.mock import AsyncMock, patch, MagicMock
import pandas as pd
import sqlite3
from market_analysis.sentiment_engine import SentimentEngine


@pytest.mark.asyncio
async def test_calculate_skew_full():
    with patch(
        "services.market_data_service.get_all_option_expiries", new_callable=AsyncMock
    ) as mock_expiries, patch(
        "services.market_data_service.get_option_chain", new_callable=AsyncMock
    ) as mock_chain, patch(
        "services.market_data_service.get_quote", new_callable=AsyncMock
    ) as mock_quote:
        mock_expiries.return_value = ["2026-06-19"]
        mock_quote.return_value = {"c": 100.0}

        calls_df = pd.DataFrame(
            {
                "strike": [105, 110, 115],
                "impliedVolatility": [0.18, 0.17, 0.16],
                "volume": [100, 100, 100],
                "openInterest": [100, 100, 100],
            }
        )
        puts_df = pd.DataFrame(
            {
                "strike": [95, 90, 85],
                "impliedVolatility": [0.22, 0.23, 0.24],
                "volume": [100, 100, 100],
                "openInterest": [100, 100, 100],
            }
        )

        class MockChain:
            def __init__(self, calls, puts):
                self.calls = calls
                self.puts = puts

        mock_chain.return_value = MockChain(calls_df, puts_df)

        with patch("market_analysis.greeks.calculate_contract_delta") as mock_delta:
            mock_delta.side_effect = [0.35, 0.25, 0.15, -0.35, -0.25, -0.15]

            result = await SentimentEngine.calculate_skew("AAPL")
            assert "skew" in result
            assert result["skew"] == pytest.approx(7.0)
            assert result["state"] != "ERROR"


@pytest.mark.asyncio
async def test_calculate_pcr():
    with patch(
        "services.market_data_service.get_all_option_expiries", new_callable=AsyncMock
    ) as mock_expiries, patch(
        "services.market_data_service.get_option_chain", new_callable=AsyncMock
    ) as mock_chain:
        mock_expiries.return_value = ["2026-06-19"]

        calls_df = pd.DataFrame(
            {"strike": [100], "openInterest": [100], "volume": [50]}
        )
        puts_df = pd.DataFrame({"strike": [100], "openInterest": [120], "volume": [60]})

        class MockChain:
            def __init__(self, calls, puts):
                self.calls = calls
                self.puts = puts

        mock_chain.return_value = MockChain(calls_df, puts_df)

        result = await SentimentEngine.calculate_pcr("AAPL")
        assert result["pcr"] == pytest.approx(1.2)


@pytest.mark.asyncio
async def test_detect_uoa():
    with patch(
        "services.market_data_service.get_all_option_expiries", new_callable=AsyncMock
    ) as mock_expiries, patch(
        "services.market_data_service.get_option_chain", new_callable=AsyncMock
    ) as mock_chain:
        mock_expiries.return_value = ["2026-06-19"]

        calls_df = pd.DataFrame(
            {
                "strike": [110],
                "volume": [2000],
                "openInterest": [100],
                "impliedVolatility": [0.25],
                "lastPrice": [1.5],
                "change": [0.2],
                "percentChange": [15.0],
            }
        )
        puts_df = pd.DataFrame({"strike": [90], "volume": [10], "openInterest": [100]})

        class MockChain:
            def __init__(self, calls, puts):
                self.calls = calls
                self.puts = puts

        mock_chain.return_value = MockChain(calls_df, puts_df)

        result = await SentimentEngine.detect_uoa("AAPL")
        assert len(result) >= 1
        assert result[0]["type"] == "CALL"
        assert result[0]["ratio"] == 20.0
        assert result[0]["trade_type"] == "BLOCK"
        assert result[0]["oi_change_net"] == 1900

        # Test with explicit columns
        calls_df["trade_type"] = ["SWEEP"]
        calls_df["oi_change_net"] = [123]
        mock_chain.return_value = MockChain(calls_df, puts_df)

        result_explicit = await SentimentEngine.detect_uoa("AAPL")
        assert len(result_explicit) >= 1
        assert result_explicit[0]["trade_type"] == "SWEEP"
        assert result_explicit[0]["oi_change_net"] == 123


def test_save_sentiment_history():
    with patch("sqlite3.connect") as mock_connect:
        mock_conn = MagicMock()
        mock_connect.return_value = mock_conn

        # Test success
        SentimentEngine.save_sentiment_history("AAPL", "PCR", 0.8)
        mock_conn.cursor().execute.assert_called()

        # Test failure (branch coverage)
        mock_connect.side_effect = Exception("DB Error")
        SentimentEngine.save_sentiment_history("AAPL", "PCR", 0.8)


@pytest.mark.asyncio
async def test_sentiment_edge_cases():
    # Test skew with no expiries
    with patch(
        "services.market_data_service.get_all_option_expiries", new_callable=AsyncMock
    ) as mock_expiries:
        mock_expiries.return_value = []
        res = await SentimentEngine.calculate_skew("AAPL")
        assert res["error"] == "No expiries"

    # Test skew data insufficient (chain=None)
    with patch(
        "services.market_data_service.get_all_option_expiries", new_callable=AsyncMock
    ) as mock_expiries, patch(
        "services.market_data_service.get_option_chain", new_callable=AsyncMock
    ) as mock_chain:
        mock_expiries.return_value = ["2026-06-19"]
        mock_chain.return_value = None
        res = await SentimentEngine.calculate_skew("AAPL")
        assert res["state"] == "N/A"

    # Test PCR with no expiries
    with patch(
        "services.market_data_service.get_all_option_expiries", new_callable=AsyncMock
    ) as mock_expiries:
        mock_expiries.return_value = []
        res = await SentimentEngine.calculate_pcr("AAPL")
        assert res["pcr"] == 0

    # Test PCR with chain=None
    with patch(
        "services.market_data_service.get_all_option_expiries", new_callable=AsyncMock
    ) as mock_expiries, patch(
        "services.market_data_service.get_option_chain", new_callable=AsyncMock
    ) as mock_chain:
        mock_expiries.return_value = ["2026-06-19"]
        mock_chain.return_value = None
        res = await SentimentEngine.calculate_pcr("AAPL")
        assert res["pcr"] == 0

    # Test detect_uoa exception
    with patch(
        "services.market_data_service.get_all_option_expiries",
        side_effect=Exception("API Error"),
    ):
        res = await SentimentEngine.detect_uoa("AAPL")
        assert res == []

    # Test skew spot_price = 0
    with patch(
        "services.market_data_service.get_all_option_expiries", new_callable=AsyncMock
    ) as mock_expiries, patch(
        "services.market_data_service.get_option_chain", new_callable=AsyncMock
    ) as mock_chain, patch(
        "services.market_data_service.get_quote", new_callable=AsyncMock
    ) as mock_quote:
        mock_expiries.return_value = ["2026-06-19"]
        mock_chain.return_value = MagicMock()
        mock_quote.return_value = {"c": 0}
        res = await SentimentEngine.calculate_skew("AAPL")
        assert res["state"] == "N/A"

    # Test skew insufficient OTM options
    with patch(
        "services.market_data_service.get_all_option_expiries", new_callable=AsyncMock
    ) as mock_expiries, patch(
        "services.market_data_service.get_option_chain", new_callable=AsyncMock
    ) as mock_chain, patch(
        "services.market_data_service.get_quote", new_callable=AsyncMock
    ) as mock_quote:
        mock_expiries.return_value = ["2026-06-19"]
        mock_quote.return_value = {"c": 100.0}

        class MockChainShort:
            def __init__(self):
                self.calls = pd.DataFrame({"strike": [101]})
                self.puts = pd.DataFrame({"strike": [99]})

        mock_chain.return_value = MockChainShort()
        res = await SentimentEngine.calculate_skew("AAPL")
        assert res["state"] == "數據不足"

    # Test PCR with call_vol = 0 (handled)
    # Test detect_uoa with no chain
    with patch(
        "services.market_data_service.get_all_option_expiries", new_callable=AsyncMock
    ) as mock_expiries, patch(
        "services.market_data_service.get_option_chain", new_callable=AsyncMock
    ) as mock_chain:
        mock_expiries.return_value = ["2026-06-19"]
        mock_chain.return_value = None
        res = await SentimentEngine.detect_uoa("AAPL")
        assert res == []

    # Test calculate_max_pain exception
    with patch(
        "services.market_data_service.get_all_option_expiries",
        side_effect=Exception("API Error"),
    ):
        res = await SentimentEngine.calculate_max_pain("AAPL")
        assert "error" in res

    # Test skew with missing OTM call (branch 74-75)
    with patch(
        "services.market_data_service.get_all_option_expiries", new_callable=AsyncMock
    ) as mock_expiries, patch(
        "services.market_data_service.get_option_chain", new_callable=AsyncMock
    ) as mock_chain, patch(
        "services.market_data_service.get_quote", new_callable=AsyncMock
    ) as mock_quote:
        mock_expiries.return_value = ["2026-06-19"]
        mock_quote.return_value = {"c": 100.0}

        class MockChainNoCall:
            def __init__(self):
                self.calls = pd.DataFrame({"strike": []})
                self.puts = pd.DataFrame({"strike": [90], "impliedVolatility": [0.2]})

        mock_chain.return_value = MockChainNoCall()
        res = await SentimentEngine.calculate_skew("AAPL")
        assert res["state"] == "數據不足"

    # Test PCR exception (branch 134-136)
    with patch(
        "services.market_data_service.get_all_option_expiries",
        side_effect=Exception("PCR Error"),
    ):
        res = await SentimentEngine.calculate_pcr("AAPL")
        assert res["state"] == "ERROR"

    # Test save_sentiment_history failure logs (branch 254-256)
    with patch("sqlite3.connect", side_effect=sqlite3.Error("Conn error")):
        SentimentEngine.save_sentiment_history("AAPL", "SKEW", 1.0)


@pytest.mark.asyncio
async def test_calculate_max_pain_full():
    with patch(
        "services.market_data_service.get_all_option_expiries", new_callable=AsyncMock
    ) as mock_expiries, patch(
        "services.market_data_service.get_option_chain", new_callable=AsyncMock
    ) as mock_chain, patch(
        "services.market_data_service.get_quote", new_callable=AsyncMock
    ) as mock_quote:
        mock_expiries.return_value = ["2026-06-19"]
        mock_quote.return_value = {"c": 100.0}

        calls_df = pd.DataFrame(
            {"strike": [90, 100, 110], "openInterest": [10, 100, 10]}
        )
        puts_df = pd.DataFrame(
            {"strike": [90, 100, 110], "openInterest": [10, 100, 10]}
        )

        class MockChain:
            def __init__(self, calls, puts):
                self.calls = calls
                self.puts = puts

        mock_chain.return_value = MockChain(calls_df, puts_df)

        result = await SentimentEngine.calculate_max_pain("AAPL")
        assert result["max_pain"] == 100


@pytest.mark.asyncio
async def test_calculate_max_pain_split_adjustment():
    with patch(
        "services.market_data_service.get_all_option_expiries", new_callable=AsyncMock
    ) as mock_expiries, patch(
        "services.market_data_service.get_option_chain", new_callable=AsyncMock
    ) as mock_chain, patch(
        "services.market_data_service.get_quote", new_callable=AsyncMock
    ) as mock_quote, patch(
        "services.market_data_service.get_stock_splits", new_callable=AsyncMock
    ) as mock_splits:
        mock_expiries.return_value = ["2026-06-19"]
        mock_quote.return_value = {"c": 80.0}
        mock_splits.return_value = pd.Series([10.0], index=[pd.Timestamp("2024-06-10")])

        # Pre-split strikes at 800.0 (equivalent to post-split 80.0)
        calls_df = pd.DataFrame(
            {"strike": [700.0, 800.0, 900.0], "openInterest": [10, 100, 10]}
        )
        puts_df = pd.DataFrame(
            {"strike": [700.0, 800.0, 900.0], "openInterest": [10, 100, 10]}
        )

        class MockChain:
            def __init__(self, calls, puts):
                self.calls = calls
                self.puts = puts

        mock_chain.return_value = MockChain(calls_df, puts_df)

        result = await SentimentEngine.calculate_max_pain("NVDA")
        # The pre-split strike of 800.0 should be adjusted by dividing by 10.0 to 80.0
        assert result["max_pain"] == 80.0
        assert result["current_price"] == 80.0
        assert result["distance_pct"] == 0.0


@pytest.mark.asyncio
async def test_iv_metrics_cache_invalidation_on_price_deviation():
    from market_analysis.sentiment_engine import _iv_cache, SentimentEngine
    from models.quant import IVMetrics
    import time

    _iv_cache.clear()

    # 1. Warm cache with ref price = 100.0
    metrics = IVMetrics(
        symbol="TSLA",
        current_iv=0.5,
        iv_rank=40.0,
        iv_percentile=40.0,
        expected_move_weekly=5.0,
        iv_status="Normal",
        is_premarket=False,
        iv_source="LIVE_IV",
        reference_spot_price=100.0,
    )
    _iv_cache["TSLA"] = (metrics, time.time() + 1800)

    # Mock market_data_service.get_quote to return a deviated price, e.g. 103.0 (> 2% deviation)
    with patch(
        "services.market_data_service.get_quote", new_callable=AsyncMock
    ) as mock_quote, patch(
        "services.market_data_service.get_all_option_expiries", new_callable=AsyncMock
    ) as mock_expiries:
        mock_quote.return_value = {"c": 103.0}
        mock_expiries.return_value = []

        res = await SentimentEngine.fetch_and_calculate_iv_metrics("TSLA")
        assert res.iv_rank != 40.0  # it was invalidated!


@pytest.mark.asyncio
async def test_kv_cache_invalidation_on_price_deviation():
    from market_analysis.sentiment_engine import SentimentEngine
    from database.cache import save_kv_cache
    from datetime import datetime

    today_str = datetime.now().strftime("%Y-%m-%d")
    cache_key = f"iv_metrics_AMD_{today_str}"

    # Save a cached value with ref price = 100.0
    cached_data = {
        "symbol": "AMD",
        "current_iv": 0.5,
        "iv_rank": 40.0,
        "iv_percentile": 40.0,
        "expected_move_weekly": 5.0,
        "iv_status": "Normal",
        "is_premarket": False,
        "iv_source": "LIVE_IV",
        "reference_spot_price": 100.0,
    }
    save_kv_cache(cache_key, cached_data)

    # Mock get_quote to return 105.0 (> 2% deviation)
    with patch(
        "services.market_data_service.get_quote", new_callable=AsyncMock
    ) as mock_quote, patch(
        "services.market_data_service.get_all_option_expiries", new_callable=AsyncMock
    ) as mock_expiries, patch("market_analysis.sentiment_engine._iv_cache", {}):
        mock_quote.return_value = {"c": 105.0}
        mock_expiries.return_value = []

        res = await SentimentEngine.fetch_and_calculate_iv_metrics("AMD")
        assert res.iv_rank != 40.0  # kv_cache was invalidated!


@pytest.mark.asyncio
async def test_max_pain_anomaly_warning_and_retry():
    from market_analysis.sentiment_engine import SentimentEngine

    with patch(
        "services.market_data_service.get_all_option_expiries", new_callable=AsyncMock
    ) as mock_expiries, patch(
        "services.market_data_service.get_option_chain", new_callable=AsyncMock
    ) as mock_chain, patch(
        "services.market_data_service.get_quote", new_callable=AsyncMock
    ) as mock_quote, patch(
        "services.market_data_service.check_and_reconcile_max_pain_anomaly",
        return_value=True,
    ) as mock_anomaly:
        mock_expiries.return_value = ["2026-06-19"]
        mock_quote.return_value = {"c": 100.0}

        calls_df = pd.DataFrame(
            {
                "strike": [50.0],
                "openInterest": [100.0],
            }  # Max Pain is at 50, which is > 30% away from 100
        )
        puts_df = pd.DataFrame({"strike": [50.0], "openInterest": [100.0]})

        class MockChain:
            def __init__(self, calls, puts):
                self.calls = calls
                self.puts = puts

        mock_chain.return_value = MockChain(calls_df, puts_df)

        await SentimentEngine.calculate_max_pain("AAPL")
        # Assert check_and_reconcile_max_pain_anomaly was called
        mock_anomaly.assert_called_once()
