import unittest
import sys
from unittest.mock import MagicMock

# Mock dependencies before importing market_analysis
sys.modules["py_vollib"] = MagicMock()
sys.modules["py_vollib.black_scholes_merton"] = MagicMock()
sys.modules["py_vollib.black_scholes_merton.greeks"] = MagicMock()
sys.modules["py_vollib.black_scholes_merton.greeks.analytical"] = MagicMock()
sys.modules["yfinance"] = MagicMock()
sys.modules["finnhub"] = MagicMock()
sys.modules["pandas_ta"] = MagicMock()
sys.modules["py_vollib_vectorized"] = MagicMock()
sys.modules["aiolimiter"] = MagicMock()
sys.modules["discord"] = MagicMock()
sys.modules["discord.ext"] = MagicMock()
sys.modules["openai"] = MagicMock()
sys.modules["pydantic"] = MagicMock()
sys.modules["pandas_market_calendars"] = MagicMock()

import pandas as pd
from market_analysis.gap_analysis import GapAnalyzer, GapType, FillStatus

class TestGapAnalysis(unittest.TestCase):
    def test_upward_gap_full_fill(self):
        data = {
            'Open': [100.0, 105.0],
            'High': [102.0, 106.0],
            'Low': [99.0, 99.0],
            'Close': [101.0, 103.0]
        }
        df = pd.DataFrame(data)
        status = GapAnalyzer.analyze_gap(df)
        
        self.assertEqual(status.gap_type, GapType.UPWARD)
        self.assertAlmostEqual(status.gap_size, 4.0)  # 105 - 101
        self.assertEqual(status.fill_status, FillStatus.FULL)
        self.assertTrue(status.is_filled)
        self.assertEqual(status.fill_percentage, 100.0)

    def test_upward_gap_partial_fill(self):
        data = {
            'Open': [100.0, 110.0],
            'High': [102.0, 112.0],
            'Low': [99.0, 105.0],
            'Close': [101.0, 108.0]
        }
        df = pd.DataFrame(data)
        status = GapAnalyzer.analyze_gap(df)
        
        self.assertEqual(status.gap_type, GapType.UPWARD)
        self.assertEqual(status.fill_status, FillStatus.PARTIAL)
        self.assertFalse(status.is_filled)
        # Gap zone: [101, 110], size 9. Low 105. Fill: 110-105 = 5. 5/9 = 55.55%
        self.assertAlmostEqual(status.fill_percentage, (5/9)*100, places=2)

    def test_downward_gap_holding(self):
        data = {
            'Open': [100.0, 95.0],
            'High': [101.0, 96.0],
            'Low': [98.0, 94.0],
            'Close': [99.0, 95.5]
        }
        df = pd.DataFrame(data)
        status = GapAnalyzer.analyze_gap(df)
        
        self.assertEqual(status.gap_type, GapType.DOWNWARD)
        self.assertAlmostEqual(status.gap_size, -4.0) # 95 - 99
        self.assertEqual(status.fill_status, FillStatus.PARTIAL) # High 96 > Open 95
        self.assertFalse(status.is_filled)

    def test_no_gap(self):
        data = {
            'Open': [100.0, 100.0],
            'High': [101.0, 101.0],
            'Low': [99.0, 99.0],
            'Close': [100.0, 100.0]
        }
        df = pd.DataFrame(data)
        status = GapAnalyzer.analyze_gap(df)
        self.assertEqual(status.gap_type, GapType.NONE)

if __name__ == '__main__':
    unittest.main()
