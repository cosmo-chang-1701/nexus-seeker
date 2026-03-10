import unittest
import pandas as pd
import numpy as np
from market_analysis.strategy import detect_ema_signals

class TestEMASignals(unittest.TestCase):
    def setUp(self):
        self.window = 21

    def test_empty_df(self):
        df = pd.DataFrame()
        self.assertIsNone(detect_ema_signals(df))

    def test_insufficient_data(self):
        df = pd.DataFrame({'Close': [100.0] * (self.window + 1)})
        self.assertIsNone(detect_ema_signals(df))

    def test_bullish_crossover(self):
        # Create data where price moves from below EMA to above EMA
        # EMA starts at 100
        prices = [100.0] * 50
        prices.append(90.0)  # p_prev = 90, ema_prev will be ~99.1
        prices.append(110.0) # p_curr = 110, ema_curr will be ~100.1
        
        df = pd.DataFrame({'Close': prices})
        signal = detect_ema_signals(df, window=self.window)
        
        self.assertIsNotNone(signal)
        self.assertEqual(signal['type'], 'CROSSOVER')
        self.assertEqual(signal['direction'], 'BULLISH')

    def test_bearish_crossover(self):
        # Create data where price moves from above EMA to below EMA
        prices = [100.0] * 50
        prices.append(110.0) # p_prev = 110, ema_prev will be ~100.9
        prices.append(90.0)  # p_curr = 90, ema_curr will be ~99.9
        
        df = pd.DataFrame({'Close': prices})
        signal = detect_ema_signals(df, window=self.window)
        
        self.assertIsNotNone(signal)
        self.assertEqual(signal['type'], 'CROSSOVER')
        self.assertEqual(signal['direction'], 'BEARISH')

    def test_support_test(self):
        # Price is above EMA and very close to it
        stable_prices = [100.0] * 50
        stable_prices.append(100.2) # 0.2% distance, within 0.5% threshold
        df = pd.DataFrame({'Close': stable_prices})
        
        signal = detect_ema_signals(df, window=20, threshold=0.01)
        
        self.assertIsNotNone(signal)
        self.assertEqual(signal['type'], 'TEST')
        self.assertEqual(signal['direction'], 'SUPPORT')

    def test_resistance_test(self):
        # Price is below EMA and very close to it
        stable_prices = [100.0] * 50
        stable_prices.append(99.8) # 0.2% below
        df = pd.DataFrame({'Close': stable_prices})
        
        signal = detect_ema_signals(df, window=20, threshold=0.01)
        
        self.assertIsNotNone(signal)
        self.assertEqual(signal['type'], 'TEST')
        self.assertEqual(signal['direction'], 'RESISTANCE')

    def test_no_signal(self):
        # Price is far from EMA
        stable_prices = [100.0] * 50
        stable_prices.append(120.0) # 20% away
        df = pd.DataFrame({'Close': stable_prices})
        
        signal = detect_ema_signals(df, window=20, threshold=0.01)
        self.assertIsNone(signal)

if __name__ == '__main__':
    unittest.main()
