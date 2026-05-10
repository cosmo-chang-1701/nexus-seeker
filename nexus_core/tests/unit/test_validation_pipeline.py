import unittest
from unittest.mock import MagicMock
from services.trading_service import TradingService
from database.user_settings import UserContext

class TestValidationPipeline(unittest.TestCase):
    def setUp(self):
        self.ts = TradingService(None)
        self.user_ctx = UserContext(
            user_id=1, capital=100000.0, risk_limit=15.0,
            total_weighted_delta=0.0, total_theta=0.0, total_gamma=0.0
        )

    def test_macro_reject_vix_ladder(self):
        data = {
            'strategy': 'STO_PUT',
            'vix_allow_signal': False,
            'vix_spot': 12.0,
            'vix_tier_name': '休兵 (Dormant)',
            'aroc': 20.0,
            'safe_qty': 1
        }
        approved, reason = self.ts._validate_trade_pipeline(self.user_ctx, data)
        self.assertFalse(approved)
        self.assertIn("MACRO_REJECT", reason)

    def test_alpha_reject_low_aroc_sto(self):
        data = {
            'strategy': 'STO_PUT',
            'vix_allow_signal': True,
            'aroc': 10.0, # < 15.0
            'safe_qty': 1
        }
        approved, reason = self.ts._validate_trade_pipeline(self.user_ctx, data)
        self.assertFalse(approved)
        self.assertIn("STO 訊號遭攔截", reason)

    def test_alpha_reject_low_aroc_bto(self):
        data = {
            'strategy': 'BTO_CALL',
            'vix_allow_signal': True,
            'aroc': 25.0, # < 30.0
            'safe_qty': 1
        }
        approved, reason = self.ts._validate_trade_pipeline(self.user_ctx, data)
        self.assertFalse(approved)
        self.assertIn("ALPHA_REJECT", reason)

    def test_risk_reject_zero_qty(self):
        data = {
            'strategy': 'STO_PUT',
            'vix_allow_signal': True,
            'aroc': 20.0,
            'safe_qty': 0
        }
        approved, reason = self.ts._validate_trade_pipeline(self.user_ctx, data)
        self.assertFalse(approved)
        self.assertIn("RISK_REJECT", reason)

    def test_approved_signal(self):
        data = {
            'strategy': 'STO_PUT',
            'vix_allow_signal': True,
            'aroc': 20.0,
            'safe_qty': 5
        }
        approved, reason = self.ts._validate_trade_pipeline(self.user_ctx, data)
        self.assertTrue(approved)
        self.assertEqual(reason, "APPROVED")
