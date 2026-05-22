import pytest
import pandas as pd
from unittest.mock import AsyncMock, patch, MagicMock
from market_analysis.volatility_inspector import VolatilityInspector


@pytest.mark.asyncio
async def test_volatility_inspector_inspect_symbol():
    # Arrange
    inspector = VolatilityInspector()
    symbol = "AAPL"

    # Mock user context
    mock_user_ctx = MagicMock()
    mock_user_ctx.cash_reserve = 10000.0
    mock_user_ctx.monthly_expense = 3000.0

    # Mock historical data (252 days)
    dates = pd.date_range(end="2026-05-21", periods=252, freq="D")
    df_hist = pd.DataFrame(
        {"Close": [150.0 + i * 0.1 for i in range(252)]}, index=dates
    )

    # Mock earnings info (within 24 hours to trigger high risk)
    mock_earnings = MagicMock()
    mock_earnings.tte_hours = 12.0

    with patch("yfinance.Ticker") as m_ticker, patch(
        "services.market_data_service.get_history_df", new_callable=AsyncMock
    ) as m_hist, patch(
        "services.calendar_service.calendar_service.get_symbol_earnings",
        new_callable=AsyncMock,
    ) as m_earnings, patch(
        "market_analysis.volatility_inspector.evaluate_ema_trend",
        new_callable=AsyncMock,
    ) as m_ema, patch("market_analysis.volatility_inspector.analyze_psq") as m_psq:
        # Mock yfinance.Ticker
        mock_ticker_instance = MagicMock()
        mock_ticker_instance.info = {
            "currentPrice": 175.0,
            "impliedVolatility": 0.80,  # High IV to trigger iv_rank > 80
        }
        m_ticker.return_value = mock_ticker_instance

        m_hist.return_value = df_hist
        m_earnings.return_value = mock_earnings
        m_ema.return_value = {"trend": "BULLISH_STRONG"}

        mock_psq_res = MagicMock()
        mock_psq_res.signal_direction = "Long"
        m_psq.return_value = mock_psq_res

        # Act
        report = await inspector.inspect_symbol(symbol, mock_user_ctx)

        # Assert
        assert report is not None
        assert report["symbol"] == symbol
        assert report["price"] == 175.0

        # Assert compatibility keys are present
        assert "iv" in report
        assert "iv_p" in report
        assert "hv" in report
        assert "status" in report
        assert "days_to_earnings" in report
        assert "stop_loss" in report
        assert "daily_theta" in report

        # Check specific values
        assert report["status"] == "高風險事件"
        assert report["days_to_earnings"] == pytest.approx(0.5)
        assert report["stop_loss"] == pytest.approx(175.0 * 0.9)
