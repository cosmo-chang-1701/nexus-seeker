import pytest
import math
import pandas as pd
import sqlite3
from unittest.mock import AsyncMock, patch, MagicMock
from market_analysis.sentiment_engine import SentimentEngine, _iv_cache
from models.quant import IVMetrics
import config

# Test Suite for Implied Volatility and IV Rank Calculations


@pytest.fixture(autouse=True)
def clear_iv_cache():
    _iv_cache.clear()
    with patch("database.cache.get_kv_cache", return_value=None), patch(
        "database.cache.save_kv_cache", new_callable=AsyncMock
    ):
        yield
    _iv_cache.clear()


@pytest.mark.asyncio
async def test_save_and_get_last_stored_iv():
    """Verify that IV values can be saved to and retrieved from the database."""
    symbol = "TEST_SAVE"
    # Ensure any previous records are cleared
    conn = sqlite3.connect(config.DB_NAME)
    cursor = conn.cursor()
    cursor.execute("DELETE FROM historical_iv WHERE symbol = ?", (symbol,))
    conn.commit()

    # Save a record
    await SentimentEngine.save_historical_iv(symbol, 0.45, "2026-05-20")

    # Retrieve last stored IV
    last_iv = SentimentEngine.get_last_stored_iv(symbol)
    assert last_iv == pytest.approx(0.45)

    # Save a newer record and verify retrieve gets the newest one
    await SentimentEngine.save_historical_iv(symbol, 0.50, "2026-05-21")
    last_iv = SentimentEngine.get_last_stored_iv(symbol)
    assert last_iv == pytest.approx(0.50)

    # Clean up
    cursor.execute("DELETE FROM historical_iv WHERE symbol = ?", (symbol,))
    conn.commit()
    conn.close()


@pytest.mark.asyncio
async def test_fetch_and_calculate_iv_metrics_success():
    """Test fetch_and_calculate_iv_metrics when yfinance successfully returns impliedVolatility."""
    symbol = "TEST_SUCCESS"

    mock_quote = {"c": 100.0}
    mock_info = {"impliedVolatility": 0.40}

    # 252 days of historical data for HV
    dates = pd.date_range(end="2026-05-21", periods=252, freq="D")
    df_hist = pd.DataFrame(
        {"Close": [100.0 + i * 0.1 for i in range(252)]}, index=dates
    )

    with patch(
        "services.market_data_service.get_quote", new_callable=AsyncMock
    ) as m_quote, patch("yfinance.Ticker") as m_ticker, patch(
        "services.market_data_service.get_history_df", new_callable=AsyncMock
    ) as m_hist, patch(
        "market_analysis.sentiment_engine.is_market_open", return_value=True
    ):
        m_quote.return_value = mock_quote
        m_hist.return_value = df_hist

        # Mock yfinance.Ticker info
        mock_ticker_instance = MagicMock()
        mock_ticker_instance.info = mock_info
        m_ticker.return_value = mock_ticker_instance

        # Calculate
        metrics = await SentimentEngine.fetch_and_calculate_iv_metrics(symbol)

        assert isinstance(metrics, IVMetrics)
        assert metrics.symbol == symbol
        assert metrics.current_iv == pytest.approx(0.40)
        assert metrics.expected_move_weekly == pytest.approx(
            100.0 * 0.40 * math.sqrt(7.0 / 365.0)
        )
        assert 0.0 <= metrics.iv_rank <= 100.0
        assert 0.0 <= metrics.iv_percentile <= 100.0
        assert metrics.iv_status in ["Low", "Normal", "High", "Extreme"]


@pytest.mark.asyncio
async def test_fetch_and_calculate_iv_metrics_cache():
    """Verify that cached IV metrics are returned without executing logic again."""
    symbol = "TEST_CACHE"

    mock_quote = {"c": 150.0}
    mock_info = {"impliedVolatility": 0.25}

    # Mock data to return
    with patch(
        "services.market_data_service.get_quote", new_callable=AsyncMock
    ) as m_quote, patch("yfinance.Ticker") as m_ticker, patch(
        "services.market_data_service.get_history_df", new_callable=AsyncMock
    ) as m_hist, patch(
        "market_analysis.sentiment_engine.is_market_open", return_value=True
    ):
        m_quote.return_value = mock_quote
        m_hist.return_value = pd.DataFrame()

        mock_ticker_instance = MagicMock()
        mock_ticker_instance.info = mock_info
        m_ticker.return_value = mock_ticker_instance

        # First call
        metrics1 = await SentimentEngine.fetch_and_calculate_iv_metrics(symbol)

        # Modify the mock responses to verify they aren't called again
        m_quote.side_effect = Exception("Should not be called")

        # Second call should fetch from cache
        metrics2 = await SentimentEngine.fetch_and_calculate_iv_metrics(symbol)

        assert metrics1.current_iv == metrics2.current_iv
        assert metrics1.expected_move_weekly == metrics2.expected_move_weekly


@pytest.mark.asyncio
async def test_fetch_and_calculate_iv_metrics_fallback_option_chain():
    """Test ATM option chain impliedVolatility fallback when ticker.info doesn't return it."""
    symbol = "TEST_FALLBACK_OPT"

    mock_quote = {"c": 100.0}
    mock_info = {}  # Empty yfinance info

    # Mock expiries and option chain
    mock_expiries = ["2026-06-19"]

    calls_df = pd.DataFrame(
        {
            "strike": [95.0, 100.0, 105.0],
            "impliedVolatility": [0.35, 0.38, 0.32],  # ATM at 100.0 is 0.38
        }
    )

    class MockChain:
        def __init__(self, calls):
            self.calls = calls
            self.puts = pd.DataFrame()

    with patch(
        "services.market_data_service.get_quote", new_callable=AsyncMock
    ) as m_quote, patch("yfinance.Ticker") as m_ticker, patch(
        "services.market_data_service.get_all_option_expiries", new_callable=AsyncMock
    ) as m_expiries, patch(
        "services.market_data_service.get_option_chain", new_callable=AsyncMock
    ) as m_chain, patch(
        "services.market_data_service.get_history_df", new_callable=AsyncMock
    ) as m_hist, patch(
        "market_analysis.sentiment_engine.is_market_open", return_value=True
    ):
        m_quote.return_value = mock_quote
        m_expiries.return_value = mock_expiries
        m_chain.return_value = MockChain(calls_df)
        m_hist.return_value = pd.DataFrame()

        mock_ticker_instance = MagicMock()
        mock_ticker_instance.info = mock_info
        m_ticker.return_value = mock_ticker_instance

        metrics = await SentimentEngine.fetch_and_calculate_iv_metrics(symbol)

        assert metrics.current_iv == pytest.approx(0.36875)
        assert metrics.expected_move_weekly == pytest.approx(
            100.0 * 0.36875 * math.sqrt(7.0 / 365.0)
        )


@pytest.mark.asyncio
async def test_fetch_and_calculate_iv_metrics_fallback_db():
    """Test database fallback when both yfinance and options chain fail."""
    symbol = "TEST_FALLBACK_DB"

    mock_quote = {"c": 80.0}
    mock_info = {}

    # Save a record in DB to trigger DB fallback
    conn = sqlite3.connect(config.DB_NAME)
    cursor = conn.cursor()
    cursor.execute("DELETE FROM historical_iv WHERE symbol = ?", (symbol,))
    conn.commit()
    await SentimentEngine.save_historical_iv(symbol, 0.28, "2026-05-20")

    with patch(
        "services.market_data_service.get_quote", new_callable=AsyncMock
    ) as m_quote, patch("yfinance.Ticker") as m_ticker, patch(
        "services.market_data_service.get_all_option_expiries", new_callable=AsyncMock
    ) as m_expiries, patch(
        "services.market_data_service.get_history_df", new_callable=AsyncMock
    ) as m_hist:
        m_quote.return_value = mock_quote
        m_expiries.return_value = []  # No expiries, forcing DB check
        m_hist.return_value = pd.DataFrame()

        mock_ticker_instance = MagicMock()
        mock_ticker_instance.info = mock_info
        m_ticker.return_value = mock_ticker_instance

        metrics = await SentimentEngine.fetch_and_calculate_iv_metrics(symbol)

        assert metrics.current_iv == pytest.approx(0.28)

    # Clean up DB
    cursor.execute("DELETE FROM historical_iv WHERE symbol = ?", (symbol,))
    conn.commit()
    conn.close()


@pytest.mark.asyncio
async def test_fetch_and_calculate_iv_metrics_fallback_hv():
    """Test Historical Volatility fallback when yfinance, options chain, and DB have no data."""
    symbol = "TEST_FALLBACK_HV"

    mock_quote = {"c": 50.0}
    mock_info = {}

    # 30 days of stock history with simple return values
    dates = pd.date_range(end="2026-05-21", periods=30, freq="D")
    prices = [50.0]
    for _ in range(29):
        # random-ish walk but deterministic: alternate +/- 1%
        prices.append(prices[-1] * (1.01 if len(prices) % 2 == 0 else 0.99))

    df_hist = pd.DataFrame({"Close": prices}, index=dates)

    with patch(
        "services.market_data_service.get_quote", new_callable=AsyncMock
    ) as m_quote, patch("yfinance.Ticker") as m_ticker, patch(
        "services.market_data_service.get_all_option_expiries", new_callable=AsyncMock
    ) as m_expiries, patch(
        "services.market_data_service.get_history_df", new_callable=AsyncMock
    ) as m_hist:
        m_quote.return_value = mock_quote
        m_expiries.return_value = []

        # First call to get_history_df is for fallback 1mo, second call is for 1y history
        m_hist.side_effect = [df_hist, df_hist]

        mock_ticker_instance = MagicMock()
        mock_ticker_instance.info = mock_info
        m_ticker.return_value = mock_ticker_instance

        metrics = await SentimentEngine.fetch_and_calculate_iv_metrics(symbol)

        assert metrics.current_iv > 0.0
        assert metrics.expected_move_weekly > 0.0
        assert metrics.iv_rank >= 0.0


@pytest.mark.asyncio
async def test_fetch_and_calculate_iv_metrics_failure_graceful_degrade():
    """Test that engine gracefully returns degraded default model on complete failure."""
    symbol = "TEST_FAILURE"

    with patch(
        "services.market_data_service.get_quote", new_callable=AsyncMock
    ) as m_quote:
        m_quote.side_effect = Exception("Market data service unavailable")

        metrics = await SentimentEngine.fetch_and_calculate_iv_metrics(symbol)

        assert isinstance(metrics, IVMetrics)
        assert metrics.symbol == symbol
        assert metrics.current_iv is None
        assert metrics.iv_rank is None
        assert metrics.iv_percentile is None
        assert metrics.expected_move_weekly is None
        assert metrics.iv_status == "Normal"


@pytest.mark.asyncio
async def test_iv_rank_and_percentile_math():
    """Explicitly verify IV Rank and IV Percentile calculations with specific test values."""
    symbol = "TEST_MATH"

    mock_quote = {"c": 100.0}
    mock_info = {"impliedVolatility": 0.40}  # current IV = 40%

    # Store historical database values: 0.20, 0.30, 0.50, 0.60
    # Current IV is 0.40.
    # Total set is [0.20, 0.30, 0.40, 0.50, 0.60] (includes current IV)
    # IV Rank: ((0.40 - 0.20) / (0.60 - 0.20)) * 100 = 50.0%
    # IV Percentile: (values < 0.40) is {0.20, 0.30} (count = 2).
    # Total count = 5. (2 / 5) * 100 = 40.0%

    conn = sqlite3.connect(config.DB_NAME)
    cursor = conn.cursor()
    cursor.execute("DELETE FROM historical_iv WHERE symbol = ?", (symbol,))
    conn.commit()

    await SentimentEngine.save_historical_iv(symbol, 0.20, "2026-05-16")
    await SentimentEngine.save_historical_iv(symbol, 0.30, "2026-05-17")
    await SentimentEngine.save_historical_iv(symbol, 0.50, "2026-05-18")
    await SentimentEngine.save_historical_iv(symbol, 0.60, "2026-05-19")

    with patch(
        "services.market_data_service.get_quote", new_callable=AsyncMock
    ) as m_quote, patch("yfinance.Ticker") as m_ticker, patch(
        "services.market_data_service.get_history_df", new_callable=AsyncMock
    ) as m_hist, patch(
        "market_analysis.sentiment_engine.is_market_open", return_value=True
    ):
        m_quote.return_value = mock_quote
        m_hist.return_value = pd.DataFrame()  # empty so we only use DB data

        mock_ticker_instance = MagicMock()
        mock_ticker_instance.info = mock_info
        m_ticker.return_value = mock_ticker_instance

        metrics = await SentimentEngine.fetch_and_calculate_iv_metrics(symbol)

        assert metrics.iv_rank == pytest.approx(50.0)
        assert metrics.iv_percentile == pytest.approx(40.0)
        assert metrics.iv_status == "Normal"  # 50% is Normal (30-70)

        # Test status boundary low (< 30)
        # If current IV is 0.25 (range 0.20 to 0.60).
        # Rank: ((0.25 - 0.20) / (0.60 - 0.20)) * 100 = 12.5%
        # Status should be "Low"
        mock_ticker_instance.info = {"impliedVolatility": 0.25}
        _iv_cache.clear()
        metrics_low = await SentimentEngine.fetch_and_calculate_iv_metrics(symbol)
        assert metrics_low.iv_status == "Low"

        # Test status boundary high (70 to 90)
        # If current IV is 0.52 (range 0.20 to 0.60).
        # Rank: ((0.52 - 0.20) / (0.60 - 0.20)) * 100 = 80.0%
        # Status should be "High"
        mock_ticker_instance.info = {"impliedVolatility": 0.52}
        _iv_cache.clear()
        metrics_high = await SentimentEngine.fetch_and_calculate_iv_metrics(symbol)
        assert metrics_high.iv_status == "High"

        # Test status boundary extreme (> 90)
        # If current IV is 0.58 (range 0.20 to 0.60).
        # Rank: ((0.58 - 0.20) / (0.60 - 0.20)) * 100 = 95.0%
        # Status should be "Extreme"
        mock_ticker_instance.info = {"impliedVolatility": 0.58}
        _iv_cache.clear()
        metrics_extreme = await SentimentEngine.fetch_and_calculate_iv_metrics(symbol)
        assert metrics_extreme.iv_status == "Extreme"

    # Clean up DB
    cursor.execute("DELETE FROM historical_iv WHERE symbol = ?", (symbol,))
    conn.commit()
    conn.close()


@pytest.mark.asyncio
async def test_fetch_and_calculate_iv_metrics_value_error_warning():
    """Test that a ValueError raised during calculation is handled as warning and returns default metrics."""
    symbol = "TEST_VALUE_ERROR"
    with patch(
        "services.market_data_service.get_quote", new_callable=AsyncMock
    ) as m_quote, patch(
        "services.market_data_service.get_history_df", new_callable=AsyncMock
    ) as m_hist:
        m_quote.return_value = {"c": 0.0}
        m_hist.return_value = pd.DataFrame()

        metrics = await SentimentEngine.fetch_and_calculate_iv_metrics(symbol)
        assert isinstance(metrics, IVMetrics)
        assert metrics.symbol == symbol
        assert metrics.current_iv is None
        assert metrics.iv_rank is None
        assert metrics.iv_percentile is None
        assert metrics.expected_move_weekly is None
        assert metrics.iv_status == "Normal"
        assert metrics.is_premarket is True


@pytest.mark.asyncio
async def test_fetch_and_calculate_iv_metrics_premarket_success():
    """Test pre-market IV calculation falling back to previous day's close in DB."""
    symbol = "TEST_PREMARKET_OK"
    mock_quote = {"c": 120.0}

    # Populate DB with historical records
    conn = sqlite3.connect(config.DB_NAME)
    cursor = conn.cursor()
    cursor.execute("DELETE FROM historical_iv WHERE symbol = ?", (symbol,))
    conn.commit()

    # Pre-populate historical records: low=0.20, high=0.60
    await SentimentEngine.save_historical_iv(symbol, 0.20, "2026-05-15")
    await SentimentEngine.save_historical_iv(symbol, 0.60, "2026-05-16")
    await SentimentEngine.save_historical_iv(
        symbol, 0.40, "2026-05-17"
    )  # last stored is 0.40
    conn.commit()

    with patch(
        "services.market_data_service.get_quote", new_callable=AsyncMock
    ) as m_quote, patch(
        "market_analysis.sentiment_engine.is_market_open", return_value=False
    ), patch(
        "services.market_data_service.get_history_df", new_callable=AsyncMock
    ) as m_hist:
        m_quote.return_value = mock_quote
        m_hist.return_value = pd.DataFrame()  # empty to rely on DB values

        _iv_cache.clear()
        metrics = await SentimentEngine.fetch_and_calculate_iv_metrics(symbol)

        assert isinstance(metrics, IVMetrics)
        assert metrics.symbol == symbol
        assert metrics.is_premarket is True
        assert metrics.current_iv == pytest.approx(
            0.40
        )  # should match the last DB record
        # History includes [0.20, 0.60, 0.40] -> min=0.20, max=0.60, current=0.40
        # Rank: ((0.40 - 0.20) / (0.60 - 0.20)) * 100 = 50.0%
        assert metrics.iv_rank == pytest.approx(50.0)

    # Clean up DB
    cursor.execute("DELETE FROM historical_iv WHERE symbol = ?", (symbol,))
    conn.commit()
    conn.close()


@pytest.mark.asyncio
async def test_fetch_and_calculate_iv_metrics_premarket_degraded():
    """Test pre-market IV calculation when DB is empty and options data fails, causing graceful degrade."""
    symbol = "TEST_PREMARKET_FAIL"
    mock_quote = {"c": 120.0}

    conn = sqlite3.connect(config.DB_NAME)
    cursor = conn.cursor()
    cursor.execute("DELETE FROM historical_iv WHERE symbol = ?", (symbol,))
    conn.commit()
    conn.close()

    with patch(
        "services.market_data_service.get_quote", new_callable=AsyncMock
    ) as m_quote, patch(
        "market_analysis.sentiment_engine.is_market_open", return_value=False
    ), patch(
        "services.market_data_service.get_history_df", new_callable=AsyncMock
    ) as m_hist, patch("yfinance.Ticker") as m_ticker:
        m_quote.return_value = mock_quote
        m_hist.return_value = pd.DataFrame()  # empty
        m_ticker.side_effect = Exception("Ticker failure")

        _iv_cache.clear()
        metrics = await SentimentEngine.fetch_and_calculate_iv_metrics(symbol)

        assert isinstance(metrics, IVMetrics)
        assert metrics.symbol == symbol
        assert metrics.is_premarket is True
        assert metrics.current_iv is None
        assert metrics.iv_rank is None


@pytest.mark.asyncio
async def test_fetch_and_calculate_iv_metrics_premarket_cache_bypassed_when_market_opens():
    """Verify that cached pre-market IV metrics are bypassed if the market is now open."""
    symbol = "TEST_BYPASS"
    mock_quote = {"c": 100.0}

    # 252 days of historical data for HV
    dates = pd.date_range(end="2026-05-21", periods=252, freq="D")
    df_hist = pd.DataFrame(
        {"Close": [100.0 + i * 0.1 for i in range(252)]}, index=dates
    )

    with patch(
        "services.market_data_service.get_quote", new_callable=AsyncMock
    ) as m_quote, patch("yfinance.Ticker") as m_ticker, patch(
        "services.market_data_service.get_history_df", new_callable=AsyncMock
    ) as m_hist, patch(
        "market_analysis.sentiment_engine.is_market_open"
    ) as m_market_open, patch(
        "market_analysis.sentiment_engine.SentimentEngine.get_last_stored_iv",
        return_value=0.40,
    ):
        # 1. First call: Premarket (is_market_open = False)
        m_market_open.return_value = False
        m_quote.return_value = mock_quote
        m_hist.return_value = df_hist

        # Call under premarket conditions
        metrics1 = await SentimentEngine.fetch_and_calculate_iv_metrics(symbol)
        assert metrics1.is_premarket is True
        assert metrics1.current_iv == pytest.approx(0.40)

        # 2. Second call: Market open (is_market_open = True)
        m_market_open.return_value = True

        # Mock yfinance.Ticker info to return live IV
        mock_ticker_instance = MagicMock()
        mock_ticker_instance.info = {"impliedVolatility": 0.45}
        m_ticker.return_value = mock_ticker_instance

        metrics2 = await SentimentEngine.fetch_and_calculate_iv_metrics(symbol)

        # It should bypass cache and fetch the new live IV (0.45)
        assert metrics2.is_premarket is False
        assert metrics2.current_iv == pytest.approx(0.45)
