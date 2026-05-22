import pytest
import pandas as pd
from unittest.mock import AsyncMock, patch, MagicMock
from services.trading_service import TradingService


@pytest.fixture
def trading_service():
    bot = MagicMock()
    return TradingService(bot)


def test_clean_market_condition_inputs(trading_service):
    # Test normal inputs
    ma20, atr, rsi = trading_service._clean_market_condition_inputs(
        100.0, 98.5, 2.5, 65.0
    )
    assert ma20 == 98.5
    assert atr == 2.5
    assert rsi == 65.0

    # Test None inputs
    ma20, atr, rsi = trading_service._clean_market_condition_inputs(
        100.0, None, None, None
    )
    assert ma20 == 100.0
    assert atr == 2.0  # 2% of price (100.0)
    assert rsi == 50.0

    # Test NaN inputs
    ma20, atr, rsi = trading_service._clean_market_condition_inputs(
        100.0, float("nan"), float("nan"), float("nan")
    )
    assert ma20 == 100.0
    assert atr == 2.0
    assert rsi == 50.0

    # Test out of bounds inputs
    ma20, atr, rsi = trading_service._clean_market_condition_inputs(
        100.0, 95.0, -1.0, 150.0
    )
    assert ma20 == 95.0
    assert atr == 2.0
    assert rsi == 50.0


@pytest.mark.asyncio
async def test_run_market_scan_unpacks_correctly(trading_service):
    # Mock database watchlist to return a list of 3-element tuples
    # (user_id, symbol, use_llm)
    mock_watchlists = [(1, "AAPL", 1)]
    mock_holdings = [{"user_id": 1, "symbol": "AAPL", "avg_cost": 150.0}]

    # Mock market data service calls
    mock_spy_df = pd.DataFrame(
        {"Close": [670.0]}, index=pd.date_range("2026-05-20", periods=1)
    )
    mock_macro = {"vix": 15.0, "oil": 75.0, "vix_change": 0.0}

    # AAPL K-line history (both 60d/1h and 1y/1d)
    # We populate it with a short history (5 rows) so SMA20/ATR14/RSI14 will evaluate to NaN,
    # thereby triggering the cleaning and fallback logic.
    mock_aapl_df_short = pd.DataFrame(
        {
            "Open": [150.0] * 5,
            "High": [152.0] * 5,
            "Low": [148.0] * 5,
            "Close": [150.0] * 5,
            "Volume": [1000] * 5,
        },
        index=pd.date_range("2026-05-20", periods=5),
    )

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
        "services.market_data_service.get_history_df", new_callable=AsyncMock
    ) as mock_get_hist, patch("database.get_full_user_context") as mock_user_ctx, patch(
        "market_analysis.portfolio.refresh_portfolio_greeks", new_callable=AsyncMock
    ) as mock_refresh:
        # mock_get_hist is called for "5d/1d" (Gap), "60d/1h" (EMA), and "1y/1d" (PSQ/scan)
        # return mock_aapl_df_short for all of them
        mock_get_hist.return_value = mock_aapl_df_short

        # Mock user context
        mock_context = MagicMock()
        mock_context.capital = 50000.0
        mock_context.total_weighted_delta = 0.0
        mock_context.option_alert_mode = 1
        mock_user_ctx.return_value = mock_context

        # Call run_market_scan
        # Since AAPL history has only 5 rows, SMA20, ATR14, RSI14 will be NaN.
        # This will test both:
        # 1. 3-element unpacking from all_watchlists.
        # 2. Indicators fallback cleaning (so MarketCondition doesn't raise validation error).
        res = await trading_service.run_market_scan(is_auto=True)

        # Assert no unpacking exception was raised, and it processed successfully
        assert isinstance(res, dict)
        mock_refresh.assert_awaited_once_with(1)
