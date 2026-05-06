import unittest
from market_analysis.risk_engine import evaluate_ditm_defense, DITMDefenseAction

class TestDITMDefense(unittest.TestCase):
    def test_hold_short_position(self):
        # Short position should always be HOLD in this function
        result = evaluate_ditm_defense(quantity=-1, current_delta=-0.9, dte=10, pnl_pct=2.0)
        self.assertEqual(result, DITMDefenseAction.HOLD)

    def test_hold_low_delta(self):
        # Delta < 0.85
        result = evaluate_ditm_defense(quantity=1, current_delta=0.80, dte=10, pnl_pct=2.0)
        self.assertEqual(result, DITMDefenseAction.HOLD)

    def test_hold_low_pnl(self):
        # PnL <= 150%
        result = evaluate_ditm_defense(quantity=1, current_delta=0.90, dte=10, pnl_pct=1.4)
        self.assertEqual(result, DITMDefenseAction.HOLD)

    def test_hold_high_dte(self):
        # DTE > 21
        result = evaluate_ditm_defense(quantity=1, current_delta=0.90, dte=25, pnl_pct=2.0)
        self.assertEqual(result, DITMDefenseAction.HOLD)

    def test_defensive_close_short_dte(self):
        # All triggers met, DTE <= 7
        result = evaluate_ditm_defense(quantity=1, current_delta=0.90, dte=5, pnl_pct=2.0)
        self.assertEqual(result, DITMDefenseAction.DEFENSIVE_CLOSE)

    def test_roll_up_out_medium_dte(self):
        # All triggers met, 7 < DTE <= 21
        result = evaluate_ditm_defense(quantity=1, current_delta=0.90, dte=15, pnl_pct=2.0)
        self.assertEqual(result, DITMDefenseAction.ROLL_UP_OUT)

    def test_put_option_roll(self):
        # Negative delta for puts
        result = evaluate_ditm_defense(quantity=1, current_delta=-0.90, dte=15, pnl_pct=2.0)
        self.assertEqual(result, DITMDefenseAction.ROLL_UP_OUT)

if __name__ == '__main__':
    unittest.main()
