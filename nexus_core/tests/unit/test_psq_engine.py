import unittest
from unittest.mock import patch
import pandas as pd
import numpy as np
import warnings
from market_analysis.psq_engine import analyze_psq, PSQResult

class TestPSQEngine(unittest.TestCase):

    def setUp(self):
        warnings.filterwarnings("ignore", category=FutureWarning)
        warnings.filterwarnings("ignore", category=RuntimeWarning)
        
        # A mock dataframe with 50 rows
        self.df = pd.DataFrame({
            'Open': [100.0] * 50,
            'High': [105.0] * 50,
            'Low': [95.0] * 50,
            'Close': [100.0] * 50
        })

    def _create_mock_bb(self, lower_val, mid_val, upper_val):
        return pd.DataFrame({
            'BBL': [lower_val] * 50,
            'BBM': [mid_val] * 50,
            'BBU': [upper_val] * 50,
            'BBB': [0] * 50,
            'BBP': [0] * 50
        })
        
    def _create_mock_kc(self, lower_val, mid_val, upper_val):
        return pd.DataFrame({
            'KCL': [lower_val] * 50,
            'KCB': [mid_val] * 50,
            'KCU': [upper_val] * 50
        })

    def test_psq_empty_or_short_df(self):
        df_short = pd.DataFrame({'Close': [100]*10})
        self.assertIsNone(analyze_psq(df_short, length=20))
        self.assertIsNone(analyze_psq(pd.DataFrame()))

    @patch('market_analysis.psq_engine.ta')
    def test_psq_high_squeeze(self, mock_ta):
        # BB is inside KC1
        mock_ta.bbands.return_value = self._create_mock_bb(98, 100, 102)
        mock_ta.kc.side_effect = [
            self._create_mock_kc(95, 100, 105), # KC1
            self._create_mock_kc(90, 100, 110), # KC2
            self._create_mock_kc(85, 100, 115)  # KC3
        ]
        mock_ta.linreg.return_value = pd.Series([0.0] * 50)
        
        result = analyze_psq(self.df, length=20)
        self.assertIsNotNone(result)
        self.assertTrue(result.is_squeezing)
        self.assertEqual(result.squeeze_level, "High")
        self.assertFalse(result.is_breakout_long)

    @patch('market_analysis.psq_engine.ta')
    def test_psq_breakout_long(self, mock_ta):
        # Previous is High Squeeze (BB inside KC1), Current is Release (BB outside all KC)
        bb_df = self._create_mock_bb(98, 100, 102)
        bb_df.loc[49, 'BBL'] = 80 # Expand BB
        bb_df.loc[49, 'BBU'] = 120
        mock_ta.bbands.return_value = bb_df
        
        mock_ta.kc.side_effect = [
            self._create_mock_kc(95, 100, 105), # KC1
            self._create_mock_kc(90, 100, 110), # KC2
            self._create_mock_kc(85, 100, 115)  # KC3
        ]
        
        # Momentum > 0
        mock_ta.linreg.return_value = pd.Series([0.0]*48 + [1.0, 5.0])
        
        result = analyze_psq(self.df, length=20)
        
        self.assertIsNotNone(result)
        self.assertFalse(result.is_squeezing)
        self.assertEqual(result.squeeze_level, "Release")
        self.assertEqual(result.momentum_color, "LightBlue") # 5.0 > 1.0 -> positive diff
        self.assertTrue(result.is_breakout_long)
        self.assertFalse(result.is_breakout_short)

    @patch('market_analysis.psq_engine.ta')
    def test_psq_breakout_short(self, mock_ta):
        # Previous is High Squeeze, Current is Release
        bb_df = self._create_mock_bb(98, 100, 102)
        bb_df.loc[49, 'BBL'] = 80
        bb_df.loc[49, 'BBU'] = 120
        mock_ta.bbands.return_value = bb_df
        
        mock_ta.kc.side_effect = [
            self._create_mock_kc(95, 100, 105),
            self._create_mock_kc(90, 100, 110),
            self._create_mock_kc(85, 100, 115)
        ]
        
        # Momentum < 0
        mock_ta.linreg.return_value = pd.Series([0.0]*48 + [-1.0, -5.0])
        
        result = analyze_psq(self.df, length=20)
        self.assertTrue(result.is_breakout_short)
        self.assertEqual(result.momentum_color, "Red") # -5.0 < -1.0 -> negative diff

    @patch('market_analysis.psq_engine.ta')
    def test_momentum_colors(self, mock_ta):
        bb_df = self._create_mock_bb(80, 100, 120)
        mock_ta.bbands.return_value = bb_df
        mock_ta.kc.side_effect = [
            self._create_mock_kc(95, 100, 105),
            self._create_mock_kc(90, 100, 110),
            self._create_mock_kc(85, 100, 115)
        ]
        
        # DarkBlue: mom > 0, diff < 0
        mock_ta.linreg.return_value = pd.Series([0.0]*48 + [5.0, 2.0])
        res1 = analyze_psq(self.df, length=20)
        self.assertEqual(res1.momentum_color, "DarkBlue")
        
        # Golden: mom < 0, diff > 0
        mock_ta.kc.side_effect = [
            self._create_mock_kc(95, 100, 105),
            self._create_mock_kc(90, 100, 110),
            self._create_mock_kc(85, 100, 115)
        ] # Reset iterator
        mock_ta.linreg.return_value = pd.Series([0.0]*48 + [-5.0, -2.0])
        res2 = analyze_psq(self.df, length=20)
        self.assertEqual(res2.momentum_color, "Golden")

if __name__ == '__main__':
    unittest.main()
