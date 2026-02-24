
import unittest
from unittest.mock import MagicMock, patch
import sys
from types import ModuleType

# --- MOCK DEPENDENCIES BEFORE IMPORTING STRATEGY ---

# Mock pandas
mock_pd = MagicMock()
sys.modules["pandas"] = mock_pd

# Mock pandas_ta
mock_ta = MagicMock()
sys.modules["pandas_ta"] = mock_ta

# Mock numpy
mock_np = MagicMock()
sys.modules["numpy"] = mock_np

# Mock yfinance
mock_yf = MagicMock()
sys.modules["yfinance"] = mock_yf

# Mock py_vollib and submodules
mock_vollib = MagicMock()
sys.modules["py_vollib"] = mock_vollib
sys.modules["py_vollib.black_scholes_merton"] = mock_vollib
sys.modules["py_vollib.black_scholes_merton.greeks"] = mock_vollib
sys.modules["py_vollib.black_scholes_merton.greeks.analytical"] = mock_vollib

# Mock config
mock_config = ModuleType("config")
mock_config.TARGET_DELTAS = {
    "STO_PUT": -0.16,
    "STO_CALL": 0.16,
    "BTO_CALL": 0.50,
    "BTO_PUT": -0.50
}
mock_config.RISK_FREE_RATE = 0.045
sys.modules["config"] = mock_config

# Now import strategy
import os
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))
from market_analysis import strategy

class TestMarketStrategy(unittest.TestCase):
    def setUp(self):
        # We need to setup what the helpers return, since we are testing strategy.analyze_symbol
        # which calls the helpers.
        # But wait, strategy.py IMPORTS these modules at the top level.
        # So we need to ensure the mocks support the operations done at import time or inside functions.
        pass

    @patch('market_analysis.strategy._calculate_technical_indicators')
    @patch('market_analysis.strategy._determine_strategy_signal')
    @patch('market_analysis.strategy._calculate_mmm')
    @patch('market_analysis.strategy._calculate_term_structure')
    @patch('market_analysis.strategy._find_target_expiry')
    @patch('market_analysis.strategy._get_best_contract_data')
    @patch('market_analysis.strategy._calculate_vertical_skew')
    @patch('market_analysis.strategy._validate_risk_and_liquidity')
    @patch('market_analysis.strategy._calculate_sizing')
    @patch('market_analysis.strategy.yf.Ticker')
    def test_analyze_symbol_flow(self, mock_ticker_cls, mock_sizing, mock_validate, mock_skew, 
                                 mock_contract, mock_expiry, mock_ts, mock_mmm, mock_signal, mock_indicators):
        
        # Setup mocks to return valid data so the flow continues to the end
        mock_ticker = MagicMock()
        mock_ticker_cls.return_value = mock_ticker
        mock_ticker.history.return_value = MagicMock() # DF
        mock_ticker.options = ["2023-11-17"]
        
        # 1. Indicators
        mock_indicators.return_value = {
            'price': 150.0,
            'rsi': 30,
            'sma20': 160.0,
            'hv_rank': 40,
            'hv_current': 0.2, # Added this
            'macd_hist': -1.0
        }
        
        # 2. Strategy Signal
        mock_signal.return_value = ("STO_PUT", "put", -0.16, 30, 45)
        
        # 3. MMM & TS
        mock_mmm.return_value = (5.0, 140.0, 160.0, 10)
        mock_ts.return_value = (1.0, "Normal")
        
        # 4. Expiry
        mock_expiry.return_value = ("2023-11-17", 30)
        
        # 5. Contract
        mock_contract_obj = MagicMock()
        mock_contract_obj.__getitem__.side_effect = lambda k: {'strike': 140, 'bid': 2.0, 'ask': 2.1, 'bs_delta': -0.15, 'impliedVolatility': 0.25}.get(k)
        mock_contract.return_value = (mock_contract_obj, MagicMock())
        
        # 6. Skew
        mock_skew.return_value = (1.1, "Neutral")
        
        # 7. Validation
        mock_validate.return_value = {
             'bid': 2.0, 'ask': 2.1, 'mid_price': 2.05, 'spread': 0.1, 'spread_ratio': 5.0,
             'vrp': 0.05, 'expected_move': 10.0, 'em_lower': 140.0, 'em_upper': 160.0,
             'suggested_hedge_strike': 130.0, 'liq_status': 'Passed', 'liq_msg': ''
        }
        
        # 8. Sizing
        mock_sizing.return_value = (20.0, 0.05, 1000.0)
        
        # ACT
        result = strategy.analyze_symbol("AAPL")
        
        # ASSERT
        self.assertIsNotNone(result)
        self.assertEqual(result['symbol'], "AAPL")
        self.assertEqual(result['strategy'], "STO_PUT")
        self.assertEqual(result['alloc_pct'], 0.05)

    def test_determine_strategy_signal(self):
        # Test the extracted function directly
        # Case 1: STO_PUT
        ind = {'price': 100, 'rsi': 30, 'hv_rank': 40, 'sma20': 110, 'macd_hist': -1}
        strat, opt, delta, min_d, max_d = strategy._determine_strategy_signal(ind)
        self.assertEqual(strat, "STO_PUT")
        
        # Case 2: None
        ind = {'price': 100, 'rsi': 50, 'hv_rank': 20, 'sma20': 100, 'macd_hist': 0}
        strat, opt, delta, min_d, max_d = strategy._determine_strategy_signal(ind)
        self.assertIsNone(strat)

if __name__ == '__main__':
    unittest.main()
