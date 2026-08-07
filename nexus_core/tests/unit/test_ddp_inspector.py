import pytest
import pandas as pd
from unittest.mock import AsyncMock, patch, MagicMock
from market_analysis.ddp_inspector import DDPInspector


@pytest.mark.asyncio
async def test_ddp_inspector_pass() -> None:
    # Arrange
    inspector = DDPInspector()
    symbol = "NVDA"

    # Mock Income Statement
    # Index needs to have 'Net Income' and 'Total Revenue'
    # Columns are 0 (current) to 5 (oldest)
    q_inc_data = {
        0: [150.0, 500.0],  # Current Q
        1: [140.0, 440.0],  # Q-1
        2: [130.0, 430.0],  # Q-2
        3: [120.0, 420.0],  # Q-3
        4: [100.0, 410.0],  # Q-4 (Year over Year)
        5: [90.0, 400.0],  # Q-5
    }
    df_q_inc = pd.DataFrame(q_inc_data, index=["Net Income", "Total Revenue"])

    # Mock ticker info
    # TTM EPS = 10.0
    # Current P/E = 15.0
    # Forward P/E = 10.0 (fwd_pe < curr_pe)
    mock_info = {
        "trailingPE": 15.0,
        "forwardPE": 10.0,
        "trailingEps": 10.0,
    }

    # Mock Historical DF
    # We want current P/E (15.0) to be < 25th percentile of 3Y P/E.
    # If trailingEps = 10.0, then historical prices should be higher so historical P/E > 15.0.
    # E.g., prices around 200 => P/E = 20.
    dates = pd.date_range(end="2026-05-21", periods=156, freq="W")
    df_hist = pd.DataFrame(
        {"Close": [200.0] * 156, "Volume": [1000] * 156}, index=dates
    )

    with patch("yfinance.Ticker") as m_ticker, patch(
        "services.market_data_service.get_history_df", new_callable=AsyncMock
    ) as m_hist:
        mock_ticker_instance = MagicMock()
        mock_ticker_instance.quarterly_income_stmt = df_q_inc
        mock_ticker_instance.info = mock_info
        m_ticker.return_value = mock_ticker_instance

        m_hist.return_value = df_hist

        # Act
        report = await inspector.inspect_symbol(symbol)

        # Assert
        assert report is not None
        assert report["is_ddp"]
        assert report["symbol"] == symbol
        assert bool(report["rev_accel"]) is True
        assert report["current_pe"] == 15.0
