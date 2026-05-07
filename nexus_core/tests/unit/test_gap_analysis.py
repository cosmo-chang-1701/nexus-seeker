import unittest
import pandas as pd
from market_analysis.gap_analysis import GapAnalyzer, GapStatus

class TestGapAnalysis(unittest.TestCase):
    def test_up_gap_holding(self):
        # Prev close 100, Open 105, Low 105 (no fill)
        df = pd.DataFrame({
            'Open': [90, 105],
            'High': [100, 110],
            'Low': [80, 105],
            'Close': [100, 108]
        }, index=pd.to_datetime(['2024-01-01', '2024-01-02']))
        df.index.name = "TSLA"
        
        result = GapAnalyzer.analyze_gap(df)
        self.assertIsNotNone(result)
        self.assertEqual(result.gap_size, 5.0)
        self.assertEqual(result.current_fill_status, GapStatus.GAP_HOLDING)

    def test_up_gap_partial_fill(self):
        # Prev close 100, Open 105, Low 101
        df = pd.DataFrame({
            'Open': [90, 105],
            'High': [100, 110],
            'Low': [80, 101],
            'Close': [100, 102]
        }, index=pd.to_datetime(['2024-01-01', '2024-01-02']))
        
        result = GapAnalyzer.analyze_gap(df)
        self.assertEqual(result.current_fill_status, GapStatus.PARTIAL_FILL)

    def test_up_gap_full_fill(self):
        # Prev close 100, Open 105, Low 99
        df = pd.DataFrame({
            'Open': [90, 105],
            'High': [100, 110],
            'Low': [80, 99],
            'Close': [100, 100]
        }, index=pd.to_datetime(['2024-01-01', '2024-01-02']))
        
        result = GapAnalyzer.analyze_gap(df)
        self.assertEqual(result.current_fill_status, GapStatus.FULL_FILL)

    def test_down_gap_holding(self):
        # Prev close 100, Open 95, High 95 (no fill)
        df = pd.DataFrame({
            'Open': [110, 95],
            'High': [120, 95],
            'Low': [100, 90],
            'Close': [100, 92]
        }, index=pd.to_datetime(['2024-01-01', '2024-01-02']))
        
        result = GapAnalyzer.analyze_gap(df)
        self.assertEqual(result.gap_size, -5.0)
        self.assertEqual(result.current_fill_status, GapStatus.GAP_HOLDING)

    def test_no_gap_filter(self):
        # Tiny gap < 0.3%
        df = pd.DataFrame({
            'Open': [100, 100.1],
            'High': [101, 101.1],
            'Low': [99, 99.1],
            'Close': [100, 100.1]
        }, index=pd.to_datetime(['2024-01-01', '2024-01-02']))
        
        result = GapAnalyzer.analyze_gap(df)
        self.assertIsNone(result)

if __name__ == '__main__':
    unittest.main()
