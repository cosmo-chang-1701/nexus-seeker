import pytest
import pandas as pd
from unittest.mock import AsyncMock, patch, MagicMock
from market_analysis.volatility_inspector import VolatilityInspector


@pytest.mark.asyncio
async def test_volatility_inspector_inspect_symbol() -> None:
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


@pytest.mark.asyncio
async def test_volatility_inspector_ddp_opportunity() -> None:
    # Arrange
    inspector = VolatilityInspector()
    symbol = "TSLA"

    # Mock user context
    mock_user_ctx = MagicMock()
    mock_user_ctx.cash_reserve = 10000.0
    mock_user_ctx.monthly_expense = 3000.0

    # Mock historical data (252 days)
    # We want HV_20 to range from something to something.
    # Let's say HV_20 min is 0.2, max is 0.6.
    dates = pd.date_range(end="2026-05-21", periods=252, freq="D")
    df_hist = pd.DataFrame(
        {"Close": [100.0 + i * 0.05 for i in range(252)]}, index=dates
    )
    # We will just patch the rolling calculation for easier exact mocking
    # Or just let pandas calculate it. For simplicity, we patch df calculation? No, it calculates in the code:
    # df["Log_Ret"] = np.log(df["Close"] / df["Close"].shift(1))
    # df["HV_20"] = df["Log_Ret"].rolling(window=20).std() * np.sqrt(252)
    # To make the test robust, we can just patch 'get_history_df' but then we have to supply a df that yields correct HV.
    # A flat or slightly growing price gives low HV.

    # Mock earnings info (far away)
    mock_earnings = MagicMock()
    mock_earnings.tte_hours = 500.0

    with patch("yfinance.Ticker") as m_ticker, patch(
        "services.market_data_service.get_history_df", new_callable=AsyncMock
    ) as m_hist, patch(
        "services.calendar_service.calendar_service.get_symbol_earnings",
        new_callable=AsyncMock,
    ) as m_earnings, patch(
        "market_analysis.volatility_inspector.evaluate_ema_trend",
        new_callable=AsyncMock,
    ) as m_ema, patch("market_analysis.volatility_inspector.analyze_psq") as m_psq:
        # Calculate expected HV_current from mock data so we can set IV appropriately
        # Let's mock the df to just have a preset HV column instead, but the code calculates it.
        # So let's provide a df with prices, and just mock info["impliedVolatility"] to be extremely low (0.01).

        m_hist.return_value = df_hist
        m_earnings.return_value = mock_earnings
        m_ema.return_value = {"trend": "BULLISH_STRONG"}

        mock_psq_res = MagicMock()
        mock_psq_res.signal_direction = "Long"
        m_psq.return_value = mock_psq_res

        # Need to know exactly what the max and min HV will be to set IV.
        # But wait, if we set IV = 0.000018, it's very likely to be < hv_current and IVR < 25%.
        mock_ticker_instance = MagicMock()
        mock_ticker_instance.info = {
            "currentPrice": 200.0,
            "impliedVolatility": 0.000018,  # Extremely low IV, less than hv_min
        }
        m_ticker.return_value = mock_ticker_instance

        # Act
        report = await inspector.inspect_symbol(symbol, mock_user_ctx)

        # Assert
        assert report is not None
        assert report["symbol"] == symbol
        assert report["status"] == "波動率極低"
        assert report["is_opportunity"] is True
        assert report["is_high_risk_vol"] is False
        assert report["strategy"] == "單邊 Call (BTO)"
