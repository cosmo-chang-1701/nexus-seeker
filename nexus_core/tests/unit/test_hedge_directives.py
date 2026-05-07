import unittest
from market_analysis.hedging import get_portfolio_exposure_status
from database.user_settings import UserContext

class TestHedgeDirectives(unittest.TestCase):
    def setUp(self):
        self.user_ctx = UserContext(
            user_id=1, capital=100000.0, risk_limit_base=15.0,
            total_weighted_delta=0.0, total_theta=0.0, total_gamma=0.0
        )

    def test_hold_within_threshold(self):
        self.user_ctx.total_weighted_delta = 30.0
        result = get_portfolio_exposure_status(self.user_ctx, spy_price=500.0, delta_threshold=50.0)
        self.assertEqual(result['directive'], "HOLD")
        self.assertIn("安全區間", result['instruction'])

    def test_reduce_exposure_positive_delta(self):
        self.user_ctx.total_weighted_delta = 75.5
        result = get_portfolio_exposure_status(self.user_ctx, spy_price=500.0, delta_threshold=50.0)
        self.assertEqual(result['directive'], "REDUCE_EXPOSURE")
        self.assertIn("賣出 75.5 股 SPY", result['instruction'])

    def test_increase_exposure_negative_delta(self):
        self.user_ctx.total_weighted_delta = -62.3
        result = get_portfolio_exposure_status(self.user_ctx, spy_price=500.0, delta_threshold=50.0)
        self.assertEqual(result['directive'], "INCREASE_EXPOSURE")
        self.assertIn("買入 62.3 股 SPY", result['instruction'])
