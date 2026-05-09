import unittest
import asyncio
import pandas as pd
import numpy as np
from unittest.mock import MagicMock, patch, AsyncMock
from market_analysis.volatility_inspector import VolatilityInspector
from database.user_settings import UserContext

class TestVolatilityInspector(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.inspector = VolatilityInspector()
        # Ensure all UserContext fields are present as per dataclass
        self.mock_user_ctx = UserContext(
            user_id=123,
            capital=100000.0,
            risk_limit_base=15.0,
            total_weighted_delta=0.0,
            total_theta=0.0,
            total_gamma=0.0,
            last_rehedge_alert_time=0,
            dynamic_tau=1.0,
            enable_option_alerts=True,
            enable_vtr=True,
            enable_psq_watchlist=False,
            enable_analyst_agent=False,
            polymarket_threshold=10000.0,
            polymarket_use_llm=True,
            polymarket_slippage=2.0,
            monthly_expense=3000.0,
            tax_reserve_rate=0.20,
            cash_reserve=10000.0
        )

    @patch("market_analysis.volatility_inspector.get_next_earnings_date")
    @patch("market_analysis.volatility_inspector.analyze_psq")
    @patch("market_analysis.volatility_inspector.evaluate_ema_trend")
    @patch("services.market_data_service.get_history_df")
    @patch("yfinance.Ticker")
    async def test_inspect_symbol_cheap_iv_success(self, mock_ticker_class, mock_get_history, mock_ema, mock_psq, mock_earnings):
        # 1. Setup Mock Ticker
        mock_ticker = MagicMock()
        mock_ticker_class.return_value = mock_ticker
        
        # IV = 15% (0.15)
        mock_ticker.info = {
            'impliedVolatility': 0.15,
            'currentPrice': 100.0
        }
        
        # 2. Mock History for HV (252+ days)
        # Create a price sequence where HV ranges significantly.
        dates = pd.date_range(end='2025-05-01', periods=300)
        # Use random walk to ensure non-zero HV
        np.random.seed(42)
        returns = np.random.normal(0.001, 0.02, 300) # 2% daily vol -> ~32% annual HV
        prices = 100 * np.exp(np.cumsum(returns))
        
        # Ensure the last part has HV > 0.15
        # 0.02 * sqrt(252) ~= 0.317
        
        df = pd.DataFrame({'Close': prices}, index=dates)
        mock_get_history.return_value = df
        
        # 3. Mock Momentum (Bullish)
        mock_ema.return_value = {"trend": "BULLISH_STRONG"}
        mock_psq.return_value = MagicMock(signal_direction="Long")
        
        # 4. Mock Earnings (Far away)
        mock_earnings.return_value = None 
        
        report = await self.inspector.inspect_symbol("AAPL", self.mock_user_ctx)
        
        self.assertIsNotNone(report)
        self.assertEqual(report['symbol'], "AAPL")
        self.assertEqual(report['strategy'], "單邊 Call (BTO)")
        # IV 0.15 should be < HV (~0.31)
        self.assertTrue(report['iv'] < report['hv'])

    @patch("market_analysis.volatility_inspector.get_next_earnings_date")
    @patch("services.market_data_service.get_history_df")
    @patch("yfinance.Ticker")
    async def test_inspect_symbol_fail_high_iv(self, mock_ticker_class, mock_get_history, mock_earnings):
        mock_ticker = MagicMock()
        mock_ticker_class.return_value = mock_ticker
        
        # Current IV = 0.80 (80%) -> Should fail
        mock_ticker.info = {'impliedVolatility': 0.80, 'currentPrice': 100.0}
        
        dates = pd.date_range(end='2025-05-01', periods=300)
        # Low volatility prices
        prices = [100.0 + i*0.001 for i in range(300)]
        df = pd.DataFrame({'Close': prices}, index=dates)
        mock_get_history.return_value = df
        
        report = await self.inspector.inspect_symbol("AAPL", self.mock_user_ctx)
        self.assertIsNone(report)

if __name__ == "__main__":
    unittest.main()
