import unittest
from market_analysis.pro_management import simulate_cc_transition

class TestProManagement(unittest.TestCase):
    def test_simulate_cc_transition(self):
        # Case: Stock price $200, realizing $1000 profit from synthetic, writing $210 CC for $5.00
        result = simulate_cc_transition(
            current_option_pnl=1000.0,
            current_stock_price=200.0,
            target_cc_strike=210.0,
            target_cc_premium=5.0
        )
        
        # Gross cost = 200 * 100 = 20000
        # Premium collected = 5 * 100 = 500
        # Net proceeds = 1000
        # Net outlay = 20000 - 1000 - 500 = 18500
        self.assertEqual(result.net_capital_outlay, 18500.0)
        self.assertEqual(result.adjusted_cost_basis, 185.0)
        
        # Yield = 500 / 18500 = 0.027027...
        # AROC = Yield * (365/30) * 100 = 32.88...
        self.assertAlmostEqual(result.projected_aroc, (500/18500) * (365/30) * 100, places=2)
        
        # Efficiency Gain = (1 - 185/200) * 100 = 7.5%
        self.assertAlmostEqual(result.capital_efficiency_gain, 7.5, places=2)

if __name__ == '__main__':
    unittest.main()
