import unittest
from unittest.mock import patch, MagicMock
from services import market_data_service
import pandas as pd
import asyncio

class TestMarketDataServiceVIX306(unittest.IsolatedAsyncioTestCase):

    @patch('services.market_data_service.yf.Ticker')
    async def test_get_vix_term_structure_backwardation(self, mock_ticker):
        # Mock yfinance data for ^VIX and ^VIX3M
        mock_vix = MagicMock()
        mock_vix.history.return_value = pd.DataFrame({'Open': [25.0], 'High': [25.0], 'Low': [25.0], 'Close': [25.0], 'Volume': [0]}, index=pd.to_datetime(['2023-10-01']))
        
        mock_vix3m = MagicMock()
        mock_vix3m.history.return_value = pd.DataFrame({'Open': [22.0], 'High': [22.0], 'Low': [22.0], 'Close': [22.0], 'Volume': [0]}, index=pd.to_datetime(['2023-10-01']))
        
        def side_effect(ticker_symbol):
            if ticker_symbol == '^VIX':
                return mock_vix
            elif ticker_symbol == '^VIX3M':
                return mock_vix3m
            return MagicMock()
            
        mock_ticker.side_effect = side_effect

        result = await market_data_service.get_vix_term_structure()
        self.assertAlmostEqual(result['vts_ratio'], 25.0 / 22.0, places=2)
        self.assertEqual(result['vts_state'], 'Backwardation')

    @patch('services.market_data_service.yf.Ticker')
    async def test_get_vix_zscores(self, mock_ticker):
        # Create a mock series of 60 days
        import numpy as np
        dates = pd.date_range(start='2023-08-01', periods=60)
        # First 59 days are 15.0, day 60 is 25.0
        closing_prices = [15.0] * 59 + [25.0]
        vix_history = pd.DataFrame({'Open': closing_prices, 'High': closing_prices, 'Low': closing_prices, 'Close': closing_prices, 'Volume': [0]*60}, index=dates)

        mock_vix = MagicMock()
        mock_vix.history.return_value = vix_history
        mock_ticker.return_value = mock_vix

        result = await market_data_service.get_vix_zscores()
        
        # Calculate expected standard deviation (sample, ddof=1 is pandas default)
        expected_z30 = (25.0 - pd.Series(closing_prices[-30:]).mean()) / pd.Series(closing_prices[-30:]).std()
        expected_z60 = (25.0 - pd.Series(closing_prices[-60:]).mean()) / pd.Series(closing_prices[-60:]).std()
        
        # Check Z-scores are correctly passed back (they should be positive since 25 > 15)
        self.assertTrue(result['zscore_30'] > 0)
        self.assertTrue(result['zscore_60'] > 0)
        # Check exactly
        self.assertAlmostEqual(result['zscore_30'], expected_z30, places=1)
        self.assertAlmostEqual(result['zscore_60'], expected_z60, places=1)

if __name__ == '__main__':
    unittest.main()
