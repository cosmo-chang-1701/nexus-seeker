import pytest
import time
from unittest.mock import AsyncMock, patch, MagicMock
from services.market_data_service import _execute_api_call


@pytest.mark.asyncio
async def test_execute_api_call_success():
    """Test _execute_api_call runs successfully under normal conditions."""
    mock_func = MagicMock(return_value="success")
    res = await _execute_api_call(mock_func, "arg1", kwarg1="val")
    assert res == "success"
    mock_func.assert_called_once_with("arg1", kwarg1="val")


@pytest.mark.asyncio
async def test_execute_api_call_cooperative_backoff():
    """Test that _execute_api_call cooperative backoff delay occurs if _rate_limit_until is set in the future."""
    mock_func = MagicMock(return_value="delayed_success")
    future_time = time.time() + 1.0

    with patch("services.market_data_service._rate_limit_until", future_time), patch(
        "asyncio.sleep", new_callable=AsyncMock
    ) as m_sleep:
        res = await _execute_api_call(mock_func)
        assert res == "delayed_success"

        # Verify that asyncio.sleep was called to wait out the rate limit
        assert m_sleep.called
        # The first sleep should be the remaining wait time
        args, kwargs = m_sleep.call_args_list[0]
        wait_time = args[0]
        assert 0.0 < wait_time <= 1.0


@pytest.mark.asyncio
async def test_execute_api_call_sets_rate_limit_on_429():
    """Test that _execute_api_call sets _rate_limit_until when hitting a 429."""
    mock_func = MagicMock()
    # Raise a 429 Exception on first call, succeed on second call
    mock_func.side_effect = [Exception("429 Too Many Requests"), "recovered"]

    # We must patch _rate_limit_until inside market_data_service so we don't pollute global state
    with patch("services.market_data_service._rate_limit_until", 0.0), patch(
        "asyncio.sleep", new_callable=AsyncMock
    ) as m_sleep:
        res = await _execute_api_call(mock_func)
        assert res == "recovered"

        # Verify sleep was called for the 429 delay
        assert m_sleep.called

        # Verify services.market_data_service._rate_limit_until was updated to a future time
        import services.market_data_service

        # Since it is patched, the actual module variable won't be modified in global namespace,
        # but the local lookup in _execute_api_call modified the patched value.
        # Let's verify that the module reference (which is patched) was set.
        assert services.market_data_service._rate_limit_until > time.time()


@pytest.mark.asyncio
async def test_get_history_df_caching_success():
    """Test that get_history_df caches results and returns cached copies on subsequent calls."""
    import pandas as pd
    from services.market_data_service import get_history_df, clear_history_cache

    clear_history_cache()

    mock_df = pd.DataFrame(
        {
            "Open": [100.0],
            "High": [105.0],
            "Low": [95.0],
            "Close": [102.0],
            "Volume": [1000],
        },
        index=pd.to_datetime(["2026-05-25"]),
    )
    mock_df.index.name = "Date"

    mock_ticker = MagicMock()
    mock_ticker.history = MagicMock(return_value=mock_df)

    with patch(
        "services.market_data_service.yf.Ticker", return_value=mock_ticker
    ) as mock_yf_ticker:
        # First call: cache miss
        df1 = await get_history_df("AAPL", period="1y", interval="1d")
        assert not df1.empty
        assert df1.loc["2026-05-25", "Close"] == 102.0
        mock_yf_ticker.assert_called_once_with("AAPL")
        mock_ticker.history.assert_called_once_with(period="1y", interval="1d")

        # Second call: cache hit
        mock_yf_ticker.reset_mock()
        mock_ticker.history.reset_mock()

        df2 = await get_history_df("AAPL", period="1y", interval="1d")
        assert not df2.empty
        assert df2.loc["2026-05-25", "Close"] == 102.0
        # Should NOT call yfinance again
        mock_yf_ticker.assert_not_called()
        mock_ticker.history.assert_not_called()


@pytest.mark.asyncio
async def test_get_history_df_cache_expiry():
    """Test that cache expires correctly after TTL."""
    import pandas as pd
    from services.market_data_service import get_history_df, clear_history_cache

    clear_history_cache()

    mock_df = pd.DataFrame(
        {
            "Open": [100.0],
            "High": [105.0],
            "Low": [95.0],
            "Close": [102.0],
            "Volume": [1000],
        },
        index=pd.to_datetime(["2026-05-25"]),
    )
    mock_df.index.name = "Date"

    mock_ticker = MagicMock()
    mock_ticker.history = MagicMock(return_value=mock_df)

    start_time = 100000.0
    with patch(
        "services.market_data_service.yf.Ticker", return_value=mock_ticker
    ), patch("time.time", return_value=start_time):
        df1 = await get_history_df("AAPL", period="1y", interval="1d")
        assert not df1.empty

    # Fast forward past TTL (6 hours = 21600 seconds)
    expiry_time = start_time + 21601.0
    with patch(
        "services.market_data_service.yf.Ticker", return_value=mock_ticker
    ) as mock_yf_ticker, patch("time.time", return_value=expiry_time):
        df2 = await get_history_df("AAPL", period="1y", interval="1d")
        assert not df2.empty
        # Should call yfinance again due to expiry
        mock_yf_ticker.assert_called_once_with("AAPL")


@pytest.mark.asyncio
async def test_get_history_df_copy_isolation():
    """Test that modifying a returned dataframe does not mutate the cached dataframe."""
    import pandas as pd
    from services.market_data_service import get_history_df, clear_history_cache

    clear_history_cache()

    mock_df = pd.DataFrame(
        {
            "Open": [100.0],
            "High": [105.0],
            "Low": [95.0],
            "Close": [102.0],
            "Volume": [1000],
        },
        index=pd.to_datetime(["2026-05-25"]),
    )
    mock_df.index.name = "Date"

    mock_ticker = MagicMock()
    mock_ticker.history = MagicMock(return_value=mock_df)

    with patch("services.market_data_service.yf.Ticker", return_value=mock_ticker):
        df1 = await get_history_df("AAPL", period="1y", interval="1d")
        assert "modified_col" not in df1.columns

        # Mutate df1
        df1["modified_col"] = 42

        # Fetch again: cache hit should return clean copy
        df2 = await get_history_df("AAPL", period="1y", interval="1d")
        assert "modified_col" not in df2.columns


@pytest.mark.asyncio
async def test_clear_history_cache():
    """Test that clear_history_cache properly invalidates the cache."""
    import pandas as pd
    from services.market_data_service import get_history_df, clear_history_cache

    clear_history_cache()

    mock_df = pd.DataFrame(
        {
            "Open": [100.0],
            "High": [105.0],
            "Low": [95.0],
            "Close": [102.0],
            "Volume": [1000],
        },
        index=pd.to_datetime(["2026-05-25"]),
    )
    mock_df.index.name = "Date"

    mock_ticker = MagicMock()
    mock_ticker.history = MagicMock(return_value=mock_df)

    with patch(
        "services.market_data_service.yf.Ticker", return_value=mock_ticker
    ) as mock_yf_ticker:
        # Fetch once to populate cache
        await get_history_df("AAPL", period="1y", interval="1d")
        mock_yf_ticker.assert_called_once()
        mock_yf_ticker.reset_mock()

        # Clear cache
        clear_history_cache()

        # Fetch again: should be cache miss
        await get_history_df("AAPL", period="1y", interval="1d")
        mock_yf_ticker.assert_called_once()


@pytest.mark.asyncio
async def test_get_all_option_expiries_caching():
    """Test that get_all_option_expiries caches the returned expiry dates list."""
    from services.market_data_service import (
        get_all_option_expiries,
        clear_options_cache,
    )

    clear_options_cache()

    mock_ticker = MagicMock()
    mock_ticker.options = ["2026-06-19", "2026-07-17"]

    with patch(
        "services.market_data_service.yf.Ticker", return_value=mock_ticker
    ) as mock_yf_ticker:
        # Miss
        expiries1 = await get_all_option_expiries("MSFT")
        assert expiries1 == ["2026-06-19", "2026-07-17"]
        mock_yf_ticker.assert_called_once_with("MSFT")

        # Hit
        mock_yf_ticker.reset_mock()
        expiries2 = await get_all_option_expiries("MSFT")
        assert expiries2 == ["2026-06-19", "2026-07-17"]
        mock_yf_ticker.assert_not_called()

        # Clear
        clear_options_cache()
        expiries3 = await get_all_option_expiries("MSFT")
        assert expiries3 == ["2026-06-19", "2026-07-17"]
        mock_yf_ticker.assert_called_once_with("MSFT")


@pytest.mark.asyncio
async def test_get_option_chain_caching():
    """Test that get_option_chain caches the chain and enforces copy-isolation on dataframes."""
    import pandas as pd
    from services.market_data_service import get_option_chain, clear_options_cache

    clear_options_cache()

    mock_calls = pd.DataFrame(
        {"strike": [150.0], "impliedVolatility": [0.3]}, index=[0]
    )
    mock_puts = pd.DataFrame(
        {"strike": [140.0], "impliedVolatility": [0.32]}, index=[0]
    )
    mock_underlying = {"symbol": "MSFT", "price": 145.0}

    mock_chain = MagicMock()
    mock_chain.calls = mock_calls
    mock_chain.puts = mock_puts
    mock_chain.underlying = mock_underlying

    mock_ticker = MagicMock()
    mock_ticker.option_chain = MagicMock(return_value=mock_chain)

    with patch(
        "services.market_data_service.yf.Ticker", return_value=mock_ticker
    ) as mock_yf_ticker:
        # First call: cache miss
        chain1 = await get_option_chain("MSFT", "2026-06-19")
        assert chain1 is not None
        assert list(chain1.calls["strike"]) == [150.0]
        mock_yf_ticker.assert_called_once_with("MSFT")
        mock_ticker.option_chain.assert_called_once_with("2026-06-19")

        # Second call: cache hit
        mock_yf_ticker.reset_mock()
        mock_ticker.option_chain.reset_mock()
        chain2 = await get_option_chain("MSFT", "2026-06-19")
        assert chain2 is not None
        assert list(chain2.calls["strike"]) == [150.0]
        mock_yf_ticker.assert_not_called()
        mock_ticker.option_chain.assert_not_called()

        # Mutate chain1 dataframe and check copy isolation
        chain1.calls["strike"] = [999.0]
        chain3 = await get_option_chain("MSFT", "2026-06-19")
        assert list(chain3.calls["strike"]) == [150.0]


@pytest.mark.asyncio
async def test_execute_api_call_respects_retry_after():
    """Test that _execute_api_call respects Retry-After header when a 429 occurs."""

    class MockResponse:
        def __init__(self, headers):
            self.headers = headers

    class MockException(Exception):
        def __init__(self, message, response):
            super().__init__(message)
            self.response = response

    mock_response = MockResponse({"Retry-After": "5.5"})
    mock_exception = MockException("429 Too Many Requests", mock_response)

    mock_func = MagicMock()
    mock_func.side_effect = [mock_exception, "recovered_after_retry"]

    with patch("services.market_data_service._rate_limit_until", 0.0), patch(
        "asyncio.sleep", new_callable=AsyncMock
    ) as m_sleep:
        res = await _execute_api_call(mock_func)
        assert res == "recovered_after_retry"

        assert m_sleep.called
        sleep_args = [args[0] for args, _ in m_sleep.call_args_list]
        assert 5.5 in sleep_args


@pytest.mark.asyncio
async def test_execute_api_call_rotates_keys():
    """Test that _execute_api_call rotates keys when hitting a 429 and multiple keys exist."""
    import services.market_data_service as mds
    from unittest.mock import MagicMock

    orig_api_key = mds.FINNHUB_API_KEY
    orig_clients = mds._clients
    orig_idx = mds._client_idx

    try:
        mds.FINNHUB_API_KEY = "dummy-key-1,dummy-key-2"
        mds._clients = []
        mds._client_idx = 0

        client1 = mds._get_client()
        client2 = mds._get_client()

        mds._client_idx = 0

        client1.quote = MagicMock(side_effect=Exception("429 Too Many Requests"))
        client2.quote = MagicMock(return_value={"c": 150.0})

        with patch("services.market_data_service._rate_limit_until", 0.0), patch(
            "asyncio.sleep", new_callable=AsyncMock
        ):
            res = await mds.get_quote("AAPL")
            assert res == {"c": 150.0}

            client1.quote.assert_called_once()
            client2.quote.assert_called_once()

    finally:
        mds.FINNHUB_API_KEY = orig_api_key
        mds._clients = orig_clients
        mds._client_idx = orig_idx


@pytest.mark.asyncio
async def test_validate_symbol(mock_symbol_validation):
    validate_symbol = mock_symbol_validation.real_fn
    import sqlite3
    import config

    # 1. Invalid input or format mismatch
    assert not await validate_symbol("")
    assert not await validate_symbol("TOOLONGTICKERNAME")
    assert not await validate_symbol("INVALID$")

    # 2. Valid quote case (returns True)
    with patch(
        "services.market_data_service.get_quote", new_callable=AsyncMock
    ) as mock_get_quote:
        mock_get_quote.return_value = {"c": 150.0}
        assert await validate_symbol("AAPL")

    # 3. Failed quote, but exists in DB (market_cache)
    with patch(
        "services.market_data_service.get_quote", new_callable=AsyncMock
    ) as mock_get_quote:
        mock_get_quote.return_value = {}

        # Connect to test DB and insert the symbol into market_cache
        conn = sqlite3.connect(config.DB_NAME)
        cursor = conn.cursor()
        cursor.execute(
            "CREATE TABLE IF NOT EXISTS market_cache (symbol TEXT PRIMARY KEY, max_pain REAL)"
        )
        cursor.execute(
            "INSERT OR REPLACE INTO market_cache (symbol, expiry, max_pain) VALUES ('XYZ', 'WEEKLY', 10.0)"
        )
        conn.commit()
        conn.close()

        assert await validate_symbol("XYZ")

    # 4. Failed quote, missing in DB, but matches standard ticker format (1-6 alphanumeric/dot/dash)
    with patch(
        "services.market_data_service.get_quote", new_callable=AsyncMock
    ) as mock_get_quote:
        mock_get_quote.return_value = {}

        # Make sure it's not in the DB
        conn = sqlite3.connect(config.DB_NAME)
        cursor = conn.cursor()
        cursor.execute("DELETE FROM market_cache WHERE symbol = 'ABC'")
        conn.commit()
        conn.close()

        assert await validate_symbol("ABC")

    # 5. Failed quote, missing in DB, doesn't match standard ticker format (too long)
    with patch(
        "services.market_data_service.get_quote", new_callable=AsyncMock
    ) as mock_get_quote:
        mock_get_quote.return_value = {}
        assert not await validate_symbol("ABC-XYZ-LONG")
