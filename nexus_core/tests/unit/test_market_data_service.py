from typing import Any
import pytest
import time
from unittest.mock import AsyncMock, patch, MagicMock
from services.market_data_service import _execute_api_call


@pytest.mark.asyncio
async def test_execute_api_call_success() -> None:
    """Test _execute_api_call runs successfully under normal conditions."""
    mock_func = MagicMock(return_value="success")
    res = await _execute_api_call(mock_func, "arg1", kwarg1="val")
    assert res == "success"
    mock_func.assert_called_once_with("arg1", kwarg1="val")


@pytest.mark.asyncio
async def test_execute_api_call_cooperative_backoff() -> None:
    """Test that _execute_api_call cooperative backoff delay occurs if _rate_limit_until is set in the future."""
    mock_func = MagicMock(return_value="delayed_success")
    future_time = time.time() + 1.0

    with (
        patch("services.market_data_service._rate_limit_until", future_time),
        patch("asyncio.sleep", new_callable=AsyncMock) as m_sleep,
    ):
        res = await _execute_api_call(mock_func)
        assert res == "delayed_success"

        # Verify that asyncio.sleep was called to wait out the rate limit
        assert m_sleep.called
        # The first sleep should be the remaining wait time
        args, kwargs = m_sleep.call_args_list[0]
        wait_time = args[0]
        assert 0.0 < wait_time <= 1.0


@pytest.mark.asyncio
async def test_execute_api_call_sets_rate_limit_on_429() -> None:
    """Test that _execute_api_call sets _rate_limit_until when hitting a 429."""
    mock_func = MagicMock()
    # Raise a 429 Exception on first call, succeed on second call
    mock_func.side_effect = [Exception("429 Too Many Requests"), "recovered"]

    # We must patch _rate_limit_until inside market_data_service so we don't pollute global state
    with (
        patch("services.market_data_service._rate_limit_until", 0.0),
        patch("asyncio.sleep", new_callable=AsyncMock) as m_sleep,
    ):
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
async def test_get_history_df_caching_success() -> None:
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
    mock_ticker.ticker = "AAPL"
    mock_ticker.history = MagicMock(return_value=mock_df)

    with (
        patch("config.TUNNEL_URL", ""),
        patch(
            "services.market_data_service.yf.Ticker", return_value=mock_ticker
        ) as mock_yf_ticker,
    ):
        # First call: cache miss
        df1 = await get_history_df("AAPL", period="1y", interval="1d")
        assert not df1.empty
        assert df1.loc["2026-05-25", "Close"] == 102.0
        mock_yf_ticker.assert_called_once_with("AAPL")
        mock_ticker.history.assert_called_once_with(
            period="1y", auto_adjust=True, repair=True, interval="1d"
        )

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
async def test_get_history_df_cache_expiry() -> None:
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
    mock_ticker.ticker = "AAPL"
    mock_ticker.history = MagicMock(return_value=mock_df)

    start_time = 100000.0
    with (
        patch("config.TUNNEL_URL", ""),
        patch("services.market_data_service.yf.Ticker", return_value=mock_ticker),
        patch("time.time", return_value=start_time),
    ):
        df1 = await get_history_df("AAPL", period="1y", interval="1d")
        assert not df1.empty

    # Fast forward past TTL (6 hours = 21600 seconds)
    expiry_time = start_time + 21601.0
    with (
        patch("config.TUNNEL_URL", ""),
        patch(
            "services.market_data_service.yf.Ticker", return_value=mock_ticker
        ) as mock_yf_ticker,
        patch("time.time", return_value=expiry_time),
    ):
        df2 = await get_history_df("AAPL", period="1y", interval="1d")
        assert not df2.empty
        # Should call yfinance again due to expiry
        mock_yf_ticker.assert_called_once_with("AAPL")


@pytest.mark.asyncio
async def test_get_history_df_copy_isolation() -> None:
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
    mock_ticker.ticker = "AAPL"
    mock_ticker.history = MagicMock(return_value=mock_df)

    with (
        patch("config.TUNNEL_URL", ""),
        patch("services.market_data_service.yf.Ticker", return_value=mock_ticker),
    ):
        df1 = await get_history_df("AAPL", period="1y", interval="1d")
        assert not df1.empty
        assert "modified_col" not in df1.columns

        # Mutate df1
        df1["modified_col"] = 42

        # Fetch again: cache hit should return clean copy
        df2 = await get_history_df("AAPL", period="1y", interval="1d")
        assert "modified_col" not in df2.columns


@pytest.mark.asyncio
async def test_clear_history_cache() -> None:
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
    mock_ticker.ticker = "AAPL"
    mock_ticker.history = MagicMock(return_value=mock_df)

    with (
        patch("config.TUNNEL_URL", ""),
        patch(
            "services.market_data_service.yf.Ticker", return_value=mock_ticker
        ) as mock_yf_ticker,
    ):
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
async def test_get_all_option_expiries_caching() -> None:
    """Test that get_all_option_expiries caches the returned expiry dates list."""
    from services.market_data_service import (
        get_all_option_expiries,
        clear_options_cache,
    )

    clear_options_cache()

    mock_ticker = MagicMock()
    mock_ticker.options = ["2026-06-19", "2026-07-17"]

    with (
        patch("config.TUNNEL_URL", ""),
        patch(
            "services.market_data_service.yf.Ticker", return_value=mock_ticker
        ) as mock_yf_ticker,
    ):
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
async def test_get_option_chain_caching() -> None:
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

    with (
        patch("config.TUNNEL_URL", ""),
        patch(
            "services.market_data_service.yf.Ticker", return_value=mock_ticker
        ) as mock_yf_ticker,
        patch(
            "services.market_data_service.get_quote",
            new_callable=AsyncMock,
            return_value={"c": 145.0},
        ),
    ):
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
        assert list(chain3.calls["strike"]) == [150.0]  # type: ignore


@pytest.mark.asyncio
async def test_get_option_chain_prefers_fresh_edge_cache_over_yfinance() -> None:
    """edge 快取命中且夠新鮮時，應直接採用，完全不呼叫 yfinance。"""
    from services.market_data_service import get_option_chain, clear_options_cache

    clear_options_cache()

    edge_payload = {
        "data": {
            "calls": [{"strike": 200.0, "openInterest": 500}],
            "puts": [{"strike": 190.0, "openInterest": 300}],
        },
        "age_seconds": 60.0,
    }

    with (
        patch(
            "services.edge_cache_client.get_cached_option_chain",
            new_callable=AsyncMock,
            return_value=edge_payload,
        ),
        patch("services.market_data_service.yf.Ticker") as mock_yf_ticker,
        patch(
            "services.market_data_service.get_quote",
            new_callable=AsyncMock,
            return_value={"c": 195.0},
        ),
    ):
        chain = await get_option_chain("NVDA", "2026-09-18")

        assert chain is not None
        assert list(chain.calls["strike"]) == [200.0]
        assert list(chain.puts["strike"]) == [190.0]
        mock_yf_ticker.assert_not_called()


@pytest.mark.asyncio
async def test_get_option_chain_falls_back_to_yfinance_when_edge_cache_stale() -> None:
    """edge 快取過舊 (age_seconds 超過新鮮度門檻) 時，應完全 fallback 回既有的
    yfinance 即時抓取路徑，行為與 edge 未部署時完全一致。"""
    import pandas as pd
    from services.market_data_service import get_option_chain, clear_options_cache

    clear_options_cache()

    stale_edge_payload = {
        "data": {
            "calls": [{"strike": 999.0, "openInterest": 1}],
            "puts": [],
        },
        "age_seconds": 7200.0,  # 遠超過新鮮度門檻
    }

    mock_calls = pd.DataFrame({"strike": [150.0], "impliedVolatility": [0.3]})
    mock_puts = pd.DataFrame({"strike": [140.0], "impliedVolatility": [0.32]})
    mock_chain = MagicMock()
    mock_chain.calls = mock_calls
    mock_chain.puts = mock_puts
    mock_chain.underlying = {"symbol": "MSFT", "price": 145.0}

    mock_ticker = MagicMock()
    mock_ticker.option_chain = MagicMock(return_value=mock_chain)

    with (
        patch("config.TUNNEL_URL", ""),
        patch(
            "services.edge_cache_client.get_cached_option_chain",
            new_callable=AsyncMock,
            return_value=stale_edge_payload,
        ),
        patch(
            "services.market_data_service.yf.Ticker", return_value=mock_ticker
        ) as mock_yf_ticker,
        patch(
            "services.market_data_service.get_quote",
            new_callable=AsyncMock,
            return_value={"c": 145.0},
        ),
    ):
        chain = await get_option_chain("MSFT", "2026-06-19")

        assert chain is not None
        assert list(chain.calls["strike"]) == [150.0]
        mock_yf_ticker.assert_called_once_with("MSFT")


@pytest.mark.asyncio
async def test_get_option_chain_falls_back_when_edge_cache_older_than_tightened_threshold() -> (
    None
):
    """新鮮度門檻已從 3600 秒收緊到 1800 秒（對齊背景輪詢 ~25-30 分鐘的設計
    週期），驗證這個收緊確實生效：一個介於新舊門檻之間（1800~3600 秒）的
    edge 快照，收緊前會被視為新鮮直接採用，收緊後應正確 fallback 回
    yfinance，而不是照單全收將近 1 小時舊的資料。"""
    import pandas as pd
    from services.market_data_service import get_option_chain, clear_options_cache

    clear_options_cache()

    stale_edge_payload = {
        "data": {
            "calls": [{"strike": 999.0, "openInterest": 1}],
            "puts": [],
        },
        "age_seconds": 2400.0,  # 40 分鐘：< 舊門檻 3600s，>= 新門檻 1800s
    }

    mock_calls = pd.DataFrame({"strike": [150.0], "impliedVolatility": [0.3]})
    mock_puts = pd.DataFrame({"strike": [140.0], "impliedVolatility": [0.32]})
    mock_chain = MagicMock()
    mock_chain.calls = mock_calls
    mock_chain.puts = mock_puts
    mock_chain.underlying = {"symbol": "MSFT", "price": 145.0}

    mock_ticker = MagicMock()
    mock_ticker.option_chain = MagicMock(return_value=mock_chain)

    with (
        patch("config.TUNNEL_URL", ""),
        patch(
            "services.edge_cache_client.get_cached_option_chain",
            new_callable=AsyncMock,
            return_value=stale_edge_payload,
        ),
        patch(
            "services.market_data_service.yf.Ticker", return_value=mock_ticker
        ) as mock_yf_ticker,
        patch(
            "services.market_data_service.get_quote",
            new_callable=AsyncMock,
            return_value={"c": 145.0},
        ),
    ):
        chain = await get_option_chain("MSFT", "2026-06-19")

        assert chain is not None
        assert list(chain.calls["strike"]) == [150.0]
        mock_yf_ticker.assert_called_once_with("MSFT")


@pytest.mark.asyncio
async def test_get_option_chain_falls_back_to_yfinance_when_edge_unreachable() -> None:
    """edge 連不上/離線時 (get_cached_option_chain 回傳 None)，應完全
    fallback 回既有的 yfinance 即時抓取路徑，watchlist 心跳不受影響。"""
    import pandas as pd
    from services.market_data_service import get_option_chain, clear_options_cache

    clear_options_cache()

    mock_calls = pd.DataFrame({"strike": [150.0], "impliedVolatility": [0.3]})
    mock_puts = pd.DataFrame({"strike": [140.0], "impliedVolatility": [0.32]})
    mock_chain = MagicMock()
    mock_chain.calls = mock_calls
    mock_chain.puts = mock_puts
    mock_chain.underlying = {"symbol": "MSFT", "price": 145.0}

    mock_ticker = MagicMock()
    mock_ticker.option_chain = MagicMock(return_value=mock_chain)

    with (
        patch("config.TUNNEL_URL", ""),
        patch(
            "services.edge_cache_client.get_cached_option_chain",
            new_callable=AsyncMock,
            return_value=None,
        ),
        patch(
            "services.market_data_service.yf.Ticker", return_value=mock_ticker
        ) as mock_yf_ticker,
        patch(
            "services.market_data_service.get_quote",
            new_callable=AsyncMock,
            return_value={"c": 145.0},
        ),
    ):
        chain = await get_option_chain("MSFT", "2026-06-19")

        assert chain is not None
        assert list(chain.calls["strike"]) == [150.0]
        mock_yf_ticker.assert_called_once_with("MSFT")


@pytest.mark.asyncio
async def test_execute_api_call_respects_retry_after() -> None:
    """Test that _execute_api_call respects Retry-After header when a 429 occurs."""

    class MockResponse:
        def __init__(self, headers: Any) -> None:
            self.headers = headers

    class MockException(Exception):
        def __init__(self, message: Any, response: Any) -> None:
            super().__init__(message)
            self.response = response

    mock_response = MockResponse({"Retry-After": "5.5"})
    mock_exception = MockException("429 Too Many Requests", mock_response)

    mock_func = MagicMock()
    mock_func.side_effect = [mock_exception, "recovered_after_retry"]

    with (
        patch("services.market_data_service._rate_limit_until", 0.0),
        patch("asyncio.sleep", new_callable=AsyncMock) as m_sleep,
    ):
        res = await _execute_api_call(mock_func)
        assert res == "recovered_after_retry"

        assert m_sleep.called
        sleep_args = [args[0] for args, _ in m_sleep.call_args_list]
        assert 5.5 in sleep_args


@pytest.mark.asyncio
async def test_get_quote_fast_fail_to_yfinance() -> None:
    """Test that get_quote bypasses Finnhub and directly calls yfinance when in rate limit cooldown."""
    import services.market_data_service as mds

    with (
        patch(
            "services.market_data_service.is_finnhub_rate_limited", return_value=True
        ),
        patch(
            "services.market_data_service.get_yfinance_quote", new_callable=AsyncMock
        ) as mock_yf_quote,
        patch(
            "services.market_data_service._execute_api_call", new_callable=AsyncMock
        ) as mock_exec_api,
    ):
        mock_yf_quote.return_value = {"c": 120.0}

        res = await mds.get_quote("AAPL")
        assert res == {"c": 120.0}

        # Verify yfinance was called and Finnhub was bypassed
        mock_yf_quote.assert_called_once_with("AAPL")
        mock_exec_api.assert_not_called()


@pytest.mark.asyncio
async def test_get_quote_futures_symbol_bypasses_finnhub() -> None:
    """Yahoo-style futures tickers (e.g. CL=F) must skip Finnhub's quote/symbol_lookup
    entirely and go straight to yfinance, since Finnhub doesn't recognize the =F suffix
    and would otherwise fast-fail with SYMBOL_NOT_FOUND."""
    import services.market_data_service as mds

    with (
        patch(
            "services.market_data_service.get_yfinance_quote", new_callable=AsyncMock
        ) as mock_yf_quote,
        patch(
            "services.market_data_service._execute_api_call", new_callable=AsyncMock
        ) as mock_exec_api,
    ):
        mock_yf_quote.return_value = {"c": 65.5}

        res = await mds.get_quote("CL=F")
        assert res == {"c": 65.5}

        mock_yf_quote.assert_called_once_with("CL=F")
        mock_exec_api.assert_not_called()


@pytest.mark.asyncio
async def test_validate_symbol(mock_symbol_validation: Any):  # type: ignore
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

        assert not await validate_symbol("ABC")


@pytest.mark.asyncio
async def test_get_vix_term_structure_success() -> None:
    """Test get_vix_term_structure successfully returns valid VTS ratio and state."""
    import pandas as pd
    from services.market_data_service import get_vix_term_structure

    vix_df = pd.DataFrame({"Close": [18.5]}, index=pd.to_datetime(["2026-05-25"]))
    vix3m_df = pd.DataFrame({"Close": [20.0]}, index=pd.to_datetime(["2026-05-25"]))

    with patch(
        "services.market_data_service.get_history_df",
        side_effect=[vix_df, vix3m_df],
    ):
        res = await get_vix_term_structure()
        assert res["is_valid"] is True
        assert res["vts_ratio"] == 0.925
        assert res["vts_state"] == "Contango"
        assert res["vix_front"] == 18.5
        assert res["vix_back"] == 20.0


@pytest.mark.asyncio
async def test_get_vix_term_structure_empty_dfs() -> None:
    """Test get_vix_term_structure safely handles empty history data."""
    import pandas as pd
    from services.market_data_service import get_vix_term_structure

    empty_df = pd.DataFrame()

    with patch(
        "services.market_data_service.get_history_df",
        side_effect=[empty_df, empty_df],
    ):
        res = await get_vix_term_structure()
        assert res["is_valid"] is False
        assert res["vts_ratio"] == 0.0
        assert res["vts_state"] == "UNKNOWN"
        assert res["vix_front"] is None


@pytest.mark.asyncio
async def test_get_vix_term_structure_invalid_values() -> None:
    """Test get_vix_term_structure rejects out-of-bounds or zero VIX values."""
    import pandas as pd
    import numpy as np
    from services.market_data_service import get_vix_term_structure

    # 1. Zero VIX
    vix_df_zero = pd.DataFrame({"Close": [0.0]}, index=pd.to_datetime(["2026-05-25"]))
    vix3m_df = pd.DataFrame({"Close": [20.0]}, index=pd.to_datetime(["2026-05-25"]))

    with patch(
        "services.market_data_service.get_history_df",
        side_effect=[vix_df_zero, vix3m_df],
    ):
        res = await get_vix_term_structure()
        assert res["is_valid"] is False
        assert res["vts_ratio"] == 0.0
        assert res["vts_state"] == "UNKNOWN"

    # 2. NaN value
    vix_df_nan = pd.DataFrame({"Close": [np.nan]}, index=pd.to_datetime(["2026-05-25"]))
    with patch(
        "services.market_data_service.get_history_df",
        side_effect=[vix_df_nan, vix3m_df],
    ):
        res_nan = await get_vix_term_structure()
        assert res_nan["is_valid"] is False
        assert res_nan["vts_ratio"] == 0.0


@pytest.mark.asyncio
async def test_get_vix_term_structure_exception() -> None:
    """Test get_vix_term_structure returns safe defaults on exception."""
    from services.market_data_service import get_vix_term_structure

    with patch(
        "services.market_data_service.get_history_df",
        side_effect=Exception("Network error"),
    ):
        res = await get_vix_term_structure()
        assert res["is_valid"] is False
        assert res["vts_ratio"] == 0.0
        assert res["vts_state"] == "UNKNOWN"


@pytest.mark.asyncio
async def test_safe_yf_history_success_with_repair() -> None:
    """Test _safe_yf_history returns DataFrame directly when repair=True succeeds
    (with Edge disabled, so it exercises the direct-yfinance fallback path)."""
    import pandas as pd
    from services.market_data_service import _safe_yf_history

    mock_df = pd.DataFrame(
        {"Close": [150.0], "Open": [148.0]},
        index=pd.to_datetime(["2026-08-17"]),
    )
    mock_ticker = MagicMock()
    mock_ticker.ticker = "AAPL"
    mock_ticker.history = MagicMock(return_value=mock_df)

    with patch("config.TUNNEL_URL", ""):
        res = await _safe_yf_history(mock_ticker, period="2d")
    assert res is not None
    assert not res.empty
    assert res.iloc[0]["Close"] == 150.0
    mock_ticker.history.assert_called_once_with(
        period="2d", auto_adjust=True, repair=True
    )


@pytest.mark.asyncio
async def test_safe_yf_history_fallback_when_repair_raises_sklearn_missing() -> None:
    """Test _safe_yf_history gracefully retries with repair=False if repair=True raises
    ModuleNotFoundError (with Edge disabled, so it exercises the direct-yfinance path)."""
    import pandas as pd
    from services.market_data_service import _safe_yf_history

    mock_df = pd.DataFrame(
        {"Close": [200.0], "Open": [198.0]},
        index=pd.to_datetime(["2026-08-17"]),
    )
    mock_ticker = MagicMock()
    mock_ticker.ticker = "TSLA"
    # First call with repair=True raises ModuleNotFoundError, second with repair=False succeeds
    mock_ticker.history = MagicMock(
        side_effect=[
            ModuleNotFoundError("No module named 'sklearn'"),
            mock_df,
        ]
    )

    with patch("config.TUNNEL_URL", ""):
        res = await _safe_yf_history(mock_ticker, period="5d", interval="1d")
    assert res is not None
    assert not res.empty
    assert res.iloc[0]["Close"] == 200.0
    assert mock_ticker.history.call_count == 2
    mock_ticker.history.assert_any_call(
        period="5d", auto_adjust=True, repair=True, interval="1d"
    )
    mock_ticker.history.assert_any_call(
        period="5d", auto_adjust=True, repair=False, interval="1d"
    )


@pytest.mark.asyncio
async def test_safe_yf_history_prefers_edge_over_direct_yfinance() -> None:
    """Test _safe_yf_history hits the Edge tunnel first and skips direct yfinance
    entirely when Edge returns usable data."""
    from services.market_data_service import _safe_yf_history

    mock_ticker = MagicMock()
    mock_ticker.ticker = "NVDA"
    mock_ticker.history = MagicMock(
        side_effect=AssertionError(
            "direct yfinance should not be called when Edge succeeds"
        )
    )

    mock_edge_response = MagicMock()
    mock_edge_response.status_code = 200
    mock_edge_response.json.return_value = {
        "status": "success",
        "data": [
            {
                "Date": "2026-08-17 00:00:00+00:00",
                "Open": 120.0,
                "High": 125.0,
                "Low": 119.0,
                "Close": 124.0,
                "Volume": 50000,
            }
        ],
    }

    mock_client = AsyncMock()
    mock_client.get = AsyncMock(return_value=mock_edge_response)
    mock_client_cls = MagicMock()
    mock_client_cls.return_value.__aenter__.return_value = mock_client

    with (
        patch("config.TUNNEL_URL", "http://edge-node:8000"),
        patch("httpx.AsyncClient", mock_client_cls),
    ):
        res = await _safe_yf_history(mock_ticker, period="1mo", interval="1d")
        assert res is not None
        assert not res.empty
        assert res.iloc[0]["Close"] == 124.0
        mock_ticker.history.assert_not_called()


@pytest.mark.asyncio
async def test_fetch_history_via_edge_handles_dst_mixed_offsets() -> None:
    """Test _fetch_history_via_edge tolerates a Date column whose rows carry
    different raw UTC offsets across a DST boundary (e.g. -0500 vs -0400),
    which plain pd.to_datetime() rejects with 'Mixed timezones detected'."""
    from services.market_data_service import _fetch_history_via_edge

    mock_edge_response = MagicMock()
    mock_edge_response.status_code = 200
    mock_edge_response.json.return_value = {
        "status": "success",
        "data": [
            {
                "Date": "2026-01-02 00:00:00-0500",
                "Open": 100.0,
                "High": 101.0,
                "Low": 99.0,
                "Close": 100.5,
                "Volume": 10000,
            },
            {
                "Date": "2026-07-20 00:00:00-0400",
                "Open": 110.0,
                "High": 111.0,
                "Low": 109.0,
                "Close": 110.5,
                "Volume": 20000,
            },
        ],
    }

    mock_client = AsyncMock()
    mock_client.get = AsyncMock(return_value=mock_edge_response)
    mock_client_cls = MagicMock()
    mock_client_cls.return_value.__aenter__.return_value = mock_client

    with (
        patch("config.TUNNEL_URL", "http://edge-node:8000"),
        patch("httpx.AsyncClient", mock_client_cls),
    ):
        df = await _fetch_history_via_edge("ETN", period="1y", interval="1d")

        assert df is not None
        assert not df.empty
        assert len(df) == 2
        assert str(df.index.tz) == "America/New_York"
        assert df.iloc[0]["Close"] == 100.5
        assert df.iloc[1]["Close"] == 110.5


@pytest.mark.asyncio
async def test_safe_yf_history_falls_back_to_direct_when_edge_fails() -> None:
    """Test _safe_yf_history falls back to direct yfinance (降級) when the Edge
    tunnel is configured but unreachable/fails."""
    import pandas as pd
    from services.market_data_service import _safe_yf_history

    mock_df = pd.DataFrame(
        {"Close": [124.0], "Open": [120.0]},
        index=pd.to_datetime(["2026-08-17"]),
    )
    mock_ticker = MagicMock()
    mock_ticker.ticker = "NVDA"
    mock_ticker.history = MagicMock(return_value=mock_df)

    mock_client = AsyncMock()
    mock_client.get = AsyncMock(side_effect=Exception("connection refused"))
    mock_client_cls = MagicMock()
    mock_client_cls.return_value.__aenter__.return_value = mock_client

    with (
        patch("config.TUNNEL_URL", "http://edge-node:8000"),
        patch("httpx.AsyncClient", mock_client_cls),
    ):
        res = await _safe_yf_history(mock_ticker, period="1mo", interval="1d")
        assert res is not None
        assert not res.empty
        assert res.iloc[0]["Close"] == 124.0
        mock_ticker.history.assert_called_once_with(
            period="1mo", auto_adjust=True, repair=True, interval="1d"
        )
