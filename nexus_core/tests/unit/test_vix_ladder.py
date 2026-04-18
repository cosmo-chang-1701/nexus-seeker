"""
Unit tests for VIX Battle Ladder integration.

Covers:
- config.py: get_vix_tier() boundary values
- strategy.py: apply_vix_ladder() signal gating and delta capping
- psq_engine.py: VIX momentum labeling
- risk_engine.py: dynamic Kelly scaling, All-in bypass, macro modifiers inversion
"""
import unittest
from unittest.mock import patch
import pandas as pd
import numpy as np
import warnings

from config import (
    VIX_LADDER_CONFIG,
    VIX_QUANTILE_BOUNDS,
    get_vix_tier,
)
from market_analysis.psq_engine import analyze_psq, PSQResult
from market_analysis.risk_engine import (
    MacroContext,
    get_macro_modifiers,
    optimize_position_risk,
    get_macro_risk_metrics,
)
from market_analysis.strategy import apply_vix_ladder


class TestGetVixTier(unittest.TestCase):
    """Test get_vix_tier() boundary values and edge cases."""

    def test_dormant_tier(self):
        tier = get_vix_tier(12.0)
        self.assertEqual(tier["name"], "休兵 (Dormant)")
        self.assertFalse(tier["allow_signal"])
        self.assertEqual(tier["sizing_multiplier"], 0.0)
        self.assertFalse(tier["vtr_entry_allowed"])

    def test_dormant_at_zero(self):
        tier = get_vix_tier(0.0)
        self.assertEqual(tier["name"], "休兵 (Dormant)")

    def test_caution_tier(self):
        tier = get_vix_tier(16.5)
        self.assertEqual(tier["name"], "少買 (Caution)")
        self.assertTrue(tier["allow_signal"])
        self.assertEqual(tier["sto_delta_cap"], -0.12)
        self.assertEqual(tier["sizing_multiplier"], 0.5)

    def test_boundary_at_15(self):
        """VIX=15.0 should be Caution, not Dormant (half-open interval)."""
        tier = get_vix_tier(15.0)
        self.assertEqual(tier["name"], "少買 (Caution)")

    def test_ready_tier(self):
        tier = get_vix_tier(20.0)
        self.assertEqual(tier["name"], "摩拳擦掌 (Ready)")
        self.assertEqual(tier["sto_delta_cap"], -0.20)
        self.assertEqual(tier["sizing_multiplier"], 1.0)

    def test_boundary_at_18(self):
        tier = get_vix_tier(18.0)
        self.assertEqual(tier["name"], "摩拳擦掌 (Ready)")

    def test_aggressive_tier(self):
        tier = get_vix_tier(27.0)
        self.assertEqual(tier["name"], "大買 (Aggressive)")
        self.assertEqual(tier["sizing_multiplier"], 1.2)

    def test_boundary_at_24(self):
        tier = get_vix_tier(24.0)
        self.assertEqual(tier["name"], "大買 (Aggressive)")

    def test_heavy_tier(self):
        tier = get_vix_tier(32.0)
        self.assertEqual(tier["name"], "重砲進場 (Heavy)")
        self.assertEqual(tier["sto_delta_cap"], -0.25)
        self.assertEqual(tier["sizing_multiplier"], 1.5)

    def test_boundary_at_30(self):
        tier = get_vix_tier(30.0)
        self.assertEqual(tier["name"], "重砲進場 (Heavy)")

    def test_allin_tier(self):
        tier = get_vix_tier(40.0)
        self.assertEqual(tier["name"], "All-in (Extreme)")
        self.assertEqual(tier["sto_delta_cap"], -0.35)
        self.assertEqual(tier["sizing_multiplier"], 2.0)
        self.assertEqual(tier["kelly_fraction_override"], 0.50)

    def test_boundary_at_35(self):
        tier = get_vix_tier(35.0)
        self.assertEqual(tier["name"], "All-in (Extreme)")

    def test_none_vix_defaults_to_ready(self):
        """None VIX should fallback to Ready tier (safe default)."""
        tier = get_vix_tier(None)
        self.assertEqual(tier["name"], "摩拳擦掌 (Ready)")

    def test_negative_vix_defaults_to_ready(self):
        tier = get_vix_tier(-5.0)
        self.assertEqual(tier["name"], "摩拳擦掌 (Ready)")

    def test_extreme_vix_value(self):
        """VIX=80 should still be All-in."""
        tier = get_vix_tier(80.0)
        self.assertEqual(tier["name"], "All-in (Extreme)")


class TestApplyVixLadder(unittest.TestCase):
    """Test the strategy-level VIX ladder wrapper."""

    def test_dormant_rejects_signals(self):
        tier = apply_vix_ladder(12.0)
        self.assertFalse(tier["allow_signal"])

    def test_caution_caps_delta(self):
        tier = apply_vix_ladder(16.0)
        self.assertEqual(tier["sto_delta_cap"], -0.12)

    def test_allin_provides_kelly_override(self):
        tier = apply_vix_ladder(38.0)
        self.assertEqual(tier["kelly_fraction_override"], 0.50)

    def test_vtr_blocked_in_dormant(self):
        tier = apply_vix_ladder(13.0)
        self.assertFalse(tier["vtr_entry_allowed"])

    def test_vtr_allowed_in_caution(self):
        tier = apply_vix_ladder(16.0)
        self.assertTrue(tier["vtr_entry_allowed"])


class TestPSQVixLabeling(unittest.TestCase):
    """Test PSQ engine VIX-aware momentum labeling."""

    def setUp(self):
        warnings.filterwarnings("ignore", category=FutureWarning)
        warnings.filterwarnings("ignore", category=RuntimeWarning)
        self.df = pd.DataFrame({
            'Open': [100.0] * 50,
            'High': [105.0] * 50,
            'Low': [95.0] * 50,
            'Close': [100.0] * 50,
        })

    def _create_mock_bb(self, lower, mid, upper):
        return pd.DataFrame({
            'BBL': [lower] * 50, 'BBM': [mid] * 50, 'BBU': [upper] * 50,
            'BBB': [0] * 50, 'BBP': [0] * 50,
        })

    def _create_mock_kc(self, lower, mid, upper):
        return pd.DataFrame({
            'KCL': [lower] * 50, 'KCB': [mid] * 50, 'KCU': [upper] * 50,
        })

    @patch('market_analysis.psq_engine.ta')
    def test_overextended_risk_label(self, mock_ta):
        """VIX < 15 + Long signal => OVEREXTENDED_RISK."""
        # Release state with positive momentum => Long direction
        bb_df = self._create_mock_bb(80, 100, 120)
        mock_ta.bbands.return_value = bb_df
        mock_ta.kc.side_effect = [
            self._create_mock_kc(95, 100, 105),
            self._create_mock_kc(90, 100, 110),
            self._create_mock_kc(85, 100, 115),
        ]
        mock_ta.linreg.return_value = pd.Series([0.0] * 48 + [1.0, 5.0])

        result = analyze_psq(self.df, length=20, vix_spot=12.0)
        self.assertIsNotNone(result)
        self.assertEqual(result.vix_momentum_label, "OVEREXTENDED_RISK")

    @patch('market_analysis.psq_engine.ta')
    def test_high_conviction_recovery_label(self, mock_ta):
        """VIX > 24.6 + Golden histogram => HIGH_CONVICTION_RECOVERY."""
        bb_df = self._create_mock_bb(80, 100, 120)
        mock_ta.bbands.return_value = bb_df
        mock_ta.kc.side_effect = [
            self._create_mock_kc(95, 100, 105),
            self._create_mock_kc(90, 100, 110),
            self._create_mock_kc(85, 100, 115),
        ]
        # Golden: mom < 0, diff > 0 (negative momentum decelerating)
        mock_ta.linreg.return_value = pd.Series([0.0] * 48 + [-5.0, -2.0])

        result = analyze_psq(self.df, length=20, vix_spot=26.0)
        self.assertIsNotNone(result)
        self.assertEqual(result.momentum_color, "Golden")
        self.assertEqual(result.vix_momentum_label, "HIGH_CONVICTION_RECOVERY")

    @patch('market_analysis.psq_engine.ta')
    def test_normal_label_when_no_vix(self, mock_ta):
        """Without vix_spot, label should be NORMAL."""
        bb_df = self._create_mock_bb(80, 100, 120)
        mock_ta.bbands.return_value = bb_df
        mock_ta.kc.side_effect = [
            self._create_mock_kc(95, 100, 105),
            self._create_mock_kc(90, 100, 110),
            self._create_mock_kc(85, 100, 115),
        ]
        mock_ta.linreg.return_value = pd.Series([0.0] * 48 + [1.0, 5.0])

        result = analyze_psq(self.df, length=20, vix_spot=None)
        self.assertIsNotNone(result)
        self.assertEqual(result.vix_momentum_label, "NORMAL")

    @patch('market_analysis.psq_engine.ta')
    def test_timeframe_note_low_vix(self, mock_ta):
        """VIX < 18 should produce timeframe advisory note."""
        bb_df = self._create_mock_bb(80, 100, 120)
        mock_ta.bbands.return_value = bb_df
        mock_ta.kc.side_effect = [
            self._create_mock_kc(95, 100, 105),
            self._create_mock_kc(90, 100, 110),
            self._create_mock_kc(85, 100, 115),
        ]
        mock_ta.linreg.return_value = pd.Series([0.0] * 48 + [-1.0, -5.0])

        result = analyze_psq(self.df, length=20, vix_spot=16.0)
        self.assertIsNotNone(result)
        self.assertIn("日K", result.vix_timeframe_note)


class TestRiskEngineMacroModifiersInversion(unittest.TestCase):
    """Test the inverted VIX weight logic in get_macro_modifiers."""

    def test_dormant_vix_zero_weight(self):
        macro = MacroContext(vix=12.0, oil_price=70.0, vix_change=0.0, vts_ratio=0.9, vix_trend_up=False)
        w_vix, _, _ = get_macro_modifiers(macro)
        self.assertEqual(w_vix, 0.0)

    def test_caution_vix_half_weight(self):
        macro = MacroContext(vix=16.0, oil_price=70.0, vix_change=0.0, vts_ratio=0.9, vix_trend_up=False)
        w_vix, _, _ = get_macro_modifiers(macro)
        self.assertEqual(w_vix, 0.5)

    def test_ready_vix_normal_weight(self):
        macro = MacroContext(vix=20.0, oil_price=70.0, vix_change=0.0, vts_ratio=0.9, vix_trend_up=False)
        w_vix, _, _ = get_macro_modifiers(macro)
        self.assertEqual(w_vix, 1.0)

    def test_aggressive_vix_expanded_weight(self):
        """VIX 24-30: offensive posture => w_vix > 1.0."""
        macro = MacroContext(vix=27.0, oil_price=70.0, vix_change=0.0, vts_ratio=0.9, vix_trend_up=False)
        w_vix, _, _ = get_macro_modifiers(macro)
        self.assertEqual(w_vix, 1.2)

    def test_heavy_vix_expanded_weight(self):
        macro = MacroContext(vix=32.0, oil_price=70.0, vix_change=0.0, vts_ratio=0.9, vix_trend_up=False)
        w_vix, _, _ = get_macro_modifiers(macro)
        self.assertEqual(w_vix, 1.5)

    def test_allin_vix_max_weight(self):
        macro = MacroContext(vix=40.0, oil_price=70.0, vix_change=0.0, vts_ratio=0.9, vix_trend_up=False)
        w_vix, _, _ = get_macro_modifiers(macro)
        self.assertEqual(w_vix, 2.0)


class TestRiskEngineDynamicKelly(unittest.TestCase):
    """Test dynamic Kelly scaling and All-in bypass in optimize_position_risk."""

    def test_no_kelly_scaling_below_threshold(self):
        """VIX=20 should not trigger Kelly scaling."""
        user_cap = 1_000_000.0
        spy_price = 500.0
        stock_iv = 0.30
        macro = MacroContext(vix=20.0, oil_price=70.0, vix_change=0.0, vts_ratio=0.9, vix_trend_up=False)

        qty_normal, _ = optimize_position_risk(
            current_delta=0, unit_weighted_delta=10.0, user_capital=user_cap,
            spy_price=spy_price, stock_iv=stock_iv, strategy="STO_PUT",
            macro_data=macro, base_risk_limit_pct=15.0, vix_spot=20.0,
        )
        self.assertTrue(qty_normal > 0)

    def test_kelly_scaling_above_upper10(self):
        """VIX=32 (> 29.5) should increase position capacity vs VIX=20."""
        user_cap = 1_000_000.0
        spy_price = 500.0
        stock_iv = 0.30

        macro_normal = MacroContext(vix=20.0, oil_price=70.0, vix_change=0.0, vts_ratio=0.9, vix_trend_up=False)
        qty_normal, _ = optimize_position_risk(
            current_delta=0, unit_weighted_delta=10.0, user_capital=user_cap,
            spy_price=spy_price, stock_iv=stock_iv, strategy="STO_PUT",
            macro_data=macro_normal, base_risk_limit_pct=15.0, vix_spot=20.0,
        )

        macro_high = MacroContext(vix=32.0, oil_price=70.0, vix_change=0.0, vts_ratio=0.9, vix_trend_up=False)
        qty_high, _ = optimize_position_risk(
            current_delta=0, unit_weighted_delta=10.0, user_capital=user_cap,
            spy_price=spy_price, stock_iv=stock_iv, strategy="STO_PUT",
            macro_data=macro_high, base_risk_limit_pct=15.0, vix_spot=32.0,
        )

        # High VIX should allow MORE contracts (offensive posture)
        self.assertTrue(qty_high > qty_normal, f"qty_high={qty_high} should > qty_normal={qty_normal}")

    def test_allin_bypass_maximizes_risk(self):
        """VIX=38 All-in mode should bypass oil/regime dampening."""
        user_cap = 1_000_000.0
        spy_price = 500.0
        stock_iv = 0.30

        # Extreme macro with high oil AND backwardation — normally would clamp hard
        macro = MacroContext(vix=38.0, oil_price=95.0, vix_change=0.0, vts_ratio=1.05, vix_trend_up=True)

        qty_allin, _ = optimize_position_risk(
            current_delta=0, unit_weighted_delta=10.0, user_capital=user_cap,
            spy_price=spy_price, stock_iv=stock_iv, strategy="STO_PUT",
            macro_data=macro, base_risk_limit_pct=15.0, vix_spot=38.0,
        )

        # Without vix_spot (old path), oil & regime would heavily reduce
        qty_old_path, _ = optimize_position_risk(
            current_delta=0, unit_weighted_delta=10.0, user_capital=user_cap,
            spy_price=spy_price, stock_iv=stock_iv, strategy="STO_PUT",
            macro_data=macro, base_risk_limit_pct=15.0, vix_spot=None,
        )

        # All-in bypass should allow significantly more contracts
        self.assertTrue(qty_allin > qty_old_path, f"qty_allin={qty_allin} should > qty_old_path={qty_old_path}")


class TestMacroRiskMetricsVixTier(unittest.TestCase):
    """Test get_macro_risk_metrics with VIX tier info."""

    def test_includes_vix_tier_name(self):
        metrics = get_macro_risk_metrics(
            total_beta_delta=50.0, total_theta=-100.0,
            total_margin_used=50000.0, total_gamma=0.5,
            user_capital=1_000_000.0, spy_price=500.0,
            vix_spot=27.0,
        )
        self.assertEqual(metrics["vix_tier_name"], "大買 (Aggressive)")

    def test_portfolio_heat_limit_scales(self):
        """Heavy tier (1.5x sizing) should have heat limit > normal 80%."""
        metrics = get_macro_risk_metrics(
            total_beta_delta=50.0, total_theta=-100.0,
            total_margin_used=50000.0, total_gamma=0.5,
            user_capital=1_000_000.0, spy_price=500.0,
            vix_spot=32.0,
        )
        self.assertEqual(metrics["portfolio_heat_limit"], 80.0 * 1.5)

    def test_no_vix_defaults(self):
        metrics = get_macro_risk_metrics(
            total_beta_delta=50.0, total_theta=-100.0,
            total_margin_used=50000.0, total_gamma=0.5,
            user_capital=1_000_000.0, spy_price=500.0,
            vix_spot=None,
        )
        self.assertEqual(metrics["vix_tier_name"], "N/A")
        self.assertEqual(metrics["portfolio_heat_limit"], 80.0)


if __name__ == '__main__':
    unittest.main()
