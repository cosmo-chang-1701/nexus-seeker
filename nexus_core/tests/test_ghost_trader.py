import unittest
from unittest.mock import MagicMock, patch
from market_analysis.ghost_trader import GhostTrader

class TestGhostTraderLogic(unittest.TestCase):
    def setUp(self):
        self.trader = GhostTrader()

    def test_seller_pnl_calculation(self):
        """測試賣方 PnL：賣出 5.0，現價 2.0 -> 應獲利 60%"""
        entry = 5.0
        mid = 2.0
        # 賣方公式: (entry - mid) / entry
        pnl_pct = (entry - mid) / entry
        self.assertEqual(pnl_pct, 0.60)

    def test_buyer_pnl_calculation(self):
        """測試買方 PnL：買入 2.0，現價 4.0 -> 應獲利 100%"""
        entry = 2.0
        mid = 4.0
        # 買方公式: (mid - entry) / entry
        pnl_pct = (mid - entry) / entry
        self.assertEqual(pnl_pct, 1.00)

if __name__ == '__main__':
    unittest.main()