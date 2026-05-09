import unittest
import asyncio
import pandas as pd
import numpy as np
from unittest.mock import MagicMock, patch, AsyncMock
from market_analysis.ddp_inspector import DDPInspector

class TestDDPInspector(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.inspector = DDPInspector()

    @patch("services.market_data_service.get_history_df")
    @patch("yfinance.Ticker")
    async def test_inspect_symbol_ddp_success(self, mock_ticker_class, mock_get_history):
        # 1. Setup Mock Ticker
        mock_ticker = MagicMock()
        mock_ticker_class.return_value = mock_ticker
        
        # Mock Income Statement (Quarterly)
        # Dates: T, T-1, T-2, T-3, T-4 (YoY), T-5
        # Net Income: 120, 110, 100, 90, 100 (YoY Growth: 20%), 95
        # Revenue: 1000, 900, 850, 800, 820 (Curr Growth: 1000/820-1=21.9%), 800 (Prev Growth: 900/800-1=12.5%) -> Accel: True
        data = {
            'Net Income': [120, 110, 105, 100, 100, 95],
            'Total Revenue': [1000, 900, 870, 830, 820, 800]
        }
        df_inc = pd.DataFrame(data).T
        mock_ticker.quarterly_income_stmt = df_inc
        mock_ticker.quarterly_financials = df_inc # fallback
        
        # Mock info
        mock_ticker.info = {
            'trailingPE': 15.0,
            'forwardPE': 12.0,
            'trailingEps': 5.0
        }
        
        # 2. Mock History for P/E Range (3Y)
        # TTM EPS = 5.0. PE = Close / 5.0. 
        # Current PE 15.0 -> Close should be 75.0
        # Hist Close: [100, 110, 120, 130, 90, 85] -> Hist PE: [20, 22, 24, 26, 18, 17]
        # Current PE 15.0 < 25th percentile (approx 17.25) -> Success
        hist_data = {'Close': [100.0, 110.0, 120.0, 130.0, 90.0, 85.0]}
        mock_get_history.return_value = pd.DataFrame(hist_data)
        
        report = await self.inspector.inspect_symbol("AAPL")
        
        self.assertIsNotNone(report)
        self.assertTrue(report['is_ddp'])
        self.assertGreater(report['eps_growth'], 0.15)
        self.assertTrue(report['rev_accel'])
        self.assertLess(report['current_pe'], report['pe_mean_3y'])
        self.assertGreaterEqual(report['confidence_score'], 80)

    @patch("services.market_data_service.get_history_df")
    @patch("yfinance.Ticker")
    async def test_inspect_symbol_ddp_fail_low_growth(self, mock_ticker_class, mock_get_history):
        mock_ticker = MagicMock()
        mock_ticker_class.return_value = mock_ticker
        
        # Net Income: 110 (Curr), 100 (YoY) -> 10% Growth < 15%
        data = {
            'Net Income': [110, 110, 105, 100, 100, 95],
            'Total Revenue': [1000, 900, 870, 830, 820, 800]
        }
        df_fail = pd.DataFrame(data).T
        mock_ticker.quarterly_income_stmt = df_fail
        mock_ticker.quarterly_financials = df_fail
        
        report = await self.inspector.inspect_symbol("AAPL")
        self.assertIsNone(report)

if __name__ == "__main__":
    unittest.main()
