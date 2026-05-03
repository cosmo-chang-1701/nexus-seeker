import unittest
from services.polymarket_service import OrderBook

class TestPolymarketService(unittest.TestCase):
    def setUp(self):
        self.ob = OrderBook(token_id="test_token")

    def test_order_book_update(self):
        self.ob.update("buy", 0.50, 1000)
        self.ob.update("sell", 0.52, 1000)
        
        self.assertEqual(self.ob.bids[0.50], 1000)
        self.assertEqual(self.ob.asks[0.52], 1000)
        
        self.ob.update("buy", 0.50, 0)
        self.assertNotIn(0.50, self.ob.bids)

    def test_mid_price_calculation(self):
        self.ob.update("buy", 0.49, 100)
        self.ob.update("sell", 0.51, 100)
        self.assertEqual(self.ob.get_mid_price(), 0.50)

    def test_slippage_threshold(self):
        # Setup deep liquidity
        # Bids: 0.49 (10000), 0.48 (20000)
        # Asks: 0.51 (10000), 0.52 (20000)
        # Mid: 0.50
        # 2% slippage target: Buy up to 0.51, Sell down to 0.49
        
        self.ob.update("buy", 0.49, 10000)
        self.ob.update("sell", 0.51, 10000)
        
        # 2% slippage Buy threshold: P_target = 0.50 * 1.02 = 0.51
        # Sum of asks where P <= 0.51: 0.51 * 10000 = 5100
        threshold = self.ob.calculate_slippage_threshold(0.02)
        self.assertEqual(threshold, 5100.0)

        # Add more liquidity
        self.ob.update("sell", 0.52, 20000)
        # 2% slippage Sell threshold: P_target = 0.50 * 0.98 = 0.49
        # Sum of bids where P >= 0.49: 0.49 * 10000 = 4900
        # Threshold is max(buy_liq, sell_liq) -> max(5100, 4900) = 5100
        threshold = self.ob.calculate_slippage_threshold(0.02)
        self.assertEqual(threshold, 5100.0)

if __name__ == "__main__":
    unittest.main()
