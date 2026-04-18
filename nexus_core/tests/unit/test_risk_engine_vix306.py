import unittest
from market_analysis.risk_engine import MacroContext, get_macro_modifiers, optimize_position_risk

class TestRiskEngineVIX306(unittest.TestCase):
    def test_get_macro_modifiers_normal(self):
        """VIX < 15: Dormant tier, w_vix = 0.0 (signals gated by strategy layer)"""
        macro = MacroContext(vix=14.0, oil_price=70.0, vix_change=0.0, vts_ratio=0.9, vix_trend_up=False)
        w_vix, w_oil, w_regime = get_macro_modifiers(macro)
        self.assertEqual(w_vix, 0.0)   # VIX < 15 -> Dormant -> 0.0
        self.assertEqual(w_oil, 1.0)   # Oil < 75 -> 1.0
        self.assertEqual(w_regime, 1.0) # Normal regime

    def test_get_macro_modifiers_backwardation(self):
        """VTS backwardation: VIX=26 -> Aggressive tier w_vix=1.2 (offensive)"""
        macro = MacroContext(vix=26.0, oil_price=70.0, vix_change=0.0, vts_ratio=1.05, vix_trend_up=False)
        w_vix, w_oil, w_regime = get_macro_modifiers(macro)
        self.assertEqual(w_vix, 1.2)   # VIX 24-30 -> Aggressive -> 1.2
        self.assertEqual(w_regime, 0.6) # vts_ratio >= 1.0 -> 0.6

    def test_get_macro_modifiers_vix_trend_up(self):
        """VIX Z-Score 看漲，觸發尾部風險"""
        macro = MacroContext(vix=18.0, oil_price=70.0, vix_change=0.0, vts_ratio=0.95, vix_trend_up=True)
        w_vix, w_oil, w_regime = get_macro_modifiers(macro)
        self.assertEqual(w_vix, 1.0)   # VIX < 20 -> 1.0
        self.assertEqual(w_regime, 0.6) # vix_trend_up -> 0.6

    def test_optimize_position_risk_high_tail_risk(self):
        """Test that is_high_tail_risk=True halves risk_limit_pct."""
        user_cap = 1000000.0
        spy_price = 500.0
        stock_iv = 0.30
        
        # Use Ready tier (VIX=20) so signals are allowed
        macro = MacroContext(vix=20.0, oil_price=70.0, vix_change=0.0, vts_ratio=0.9, vix_trend_up=False)
        
        # Scenario 1: Normal
        qty_normal, hedge_normal = optimize_position_risk(
            current_delta=0, unit_weighted_delta=10.0, user_capital=user_cap, 
            spy_price=spy_price, stock_iv=stock_iv, strategy="STO_PUT",
            macro_data=macro, base_risk_limit_pct=15.0, is_high_tail_risk=False,
            vix_spot=20.0
        )
        
        # Scenario 2: High Tail Risk
        qty_high_risk, hedge_high_risk = optimize_position_risk(
            current_delta=0, unit_weighted_delta=10.0, user_capital=user_cap, 
            spy_price=spy_price, stock_iv=stock_iv, strategy="STO_PUT",
            macro_data=macro, base_risk_limit_pct=15.0, is_high_tail_risk=True,
            vix_spot=20.0
        )

        # In High Tail Risk, safe_qty should be roughly half
        self.assertTrue(qty_high_risk <= qty_normal / 2)
        self.assertTrue(qty_normal > 0) # Ensure it's not simply 0 < 0

    def test_optimize_position_risk_with_macro_regime(self):
        """Test that extreme macro regime reduces position capacity."""
        user_cap = 1000000.0
        spy_price = 500.0
        stock_iv = 0.30
        
        # Normal macro (Ready tier)
        macro_normal = MacroContext(vix=20.0, oil_price=70.0, vix_change=0.0, vts_ratio=0.9, vix_trend_up=False)
        qty_normal, _ = optimize_position_risk(
            current_delta=0, unit_weighted_delta=10.0, user_capital=user_cap, 
            spy_price=spy_price, stock_iv=stock_iv, strategy="STO_PUT",
            macro_data=macro_normal, base_risk_limit_pct=15.0, is_high_tail_risk=False,
            vix_spot=20.0
        )
        
        # Same VIX but with backwardation + trend up (regime dampening)
        macro_extreme = MacroContext(vix=20.0, oil_price=70.0, vix_change=0.0, vts_ratio=1.05, vix_trend_up=True)
        qty_extreme, _ = optimize_position_risk(
            current_delta=0, unit_weighted_delta=10.0, user_capital=user_cap, 
            spy_price=spy_price, stock_iv=stock_iv, strategy="STO_PUT",
            macro_data=macro_extreme, base_risk_limit_pct=15.0, is_high_tail_risk=False,
            vix_spot=20.0
        )

        self.assertTrue(qty_extreme < qty_normal)
        self.assertTrue(qty_normal > 0)

if __name__ == '__main__':
    unittest.main()
