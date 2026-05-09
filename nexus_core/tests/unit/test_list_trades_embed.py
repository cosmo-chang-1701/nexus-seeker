import unittest
import discord
from datetime import datetime, timezone
from cogs.embed_builder import create_trades_embed

class TestListTradesEmbed(unittest.TestCase):
    def test_empty_trades(self):
        embed = create_trades_embed([])
        self.assertEqual(embed.title, "📊 Nexus Seeker | 實單持倉清單")
        self.assertEqual(embed.description, "📭 目前無持倉紀錄。")

    def test_trades_with_quotes(self):
        # row: (id, symbol, opt_type, strike, expiry, entry_price, quantity, stock_cost, weighted_delta, theta, gamma, trade_category)
        rows = [
            (1, "AAPL", "call", 150.0, "2024-06-21", 5.0, 2, 0.0, 0.5, -10.0, 0.01, "SPECULATIVE"),
            (2, "TSLA", "put", 180.0, "2024-06-21", 8.0, -1, 0.0, -0.4, -15.0, 0.02, "SPECULATIVE")
        ]
        stock_quotes = {"AAPL": 155.0, "TSLA": 175.0}
        total_capital = 100000.0
        
        embed = create_trades_embed(rows, stock_quotes, total_capital)
        
        self.assertEqual(embed.title, "📊 Nexus Seeker | 實單持倉清單")
        self.assertEqual(len(embed.fields), 2)
        
        # Check details field
        details = embed.fields[0].value
        self.assertIn("AAPL", details)
        self.assertIn("TSLA", details)
        self.assertIn("ITM", details) # AAPL 155 > 150 Call, TSLA 175 < 180 Put
        
        # Check summary field
        summary = embed.fields[1].value
        # Total cost: (5.0 * 2 * 100) + (8.0 * 1 * 100) = 1000 + 800 = 1800
        self.assertIn("$1,800.00", summary)
        # Ratio: 1800 / 100000 = 1.8%
        self.assertIn("1.8%", summary)

if __name__ == '__main__':
    unittest.main()
