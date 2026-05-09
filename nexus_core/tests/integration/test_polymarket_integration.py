import asyncio
import datetime as dt
import tempfile
import unittest
import json
import sqlite3
from pathlib import Path
from unittest.mock import AsyncMock, patch, MagicMock

from database.core import run_migrations
from database.user_settings import upsert_user_config
from services.polymarket_service import PolymarketService, OrderBook

class PolymarketIntegrationTestCase(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        # Setup isolated database
        self._tmpdir = tempfile.TemporaryDirectory()
        self.db_path = str(Path(self._tmpdir.name) / "polymarket_test.db")

        # Patch DB_NAME in all modules that use it
        self._db_patchers = [
            patch("database.core.DB_NAME", self.db_path),
            patch("database.user_settings.DB_NAME", self.db_path),
            patch("database.portfolio.DB_NAME", self.db_path),
            patch("database.virtual_trading.DB_NAME", self.db_path),
        ]

        for patcher in self._db_patchers:
            patcher.start()

        run_migrations()
        
        # Mock Bot
        self.mock_bot = MagicMock()
        self.mock_bot.queue_dm = AsyncMock()
        
        # Initialize Service
        self.service = PolymarketService(self.mock_bot)

    def tearDown(self):
        for patcher in reversed(self._db_patchers):
            patcher.stop()
        self._tmpdir.cleanup()

    @patch("services.polymarket_service.generate_polymarket_summary", new_callable=AsyncMock)
    @patch("services.polymarket_service.httpx.AsyncClient")
    async def test_whale_trade_flow(self, mock_client_class, mock_generate_summary):
        # ... (rest of the test method)
        # 1. Setup Mock User
        user_id = 12345
        upsert_user_config(user_id, polymarket_threshold=5000.0, polymarket_use_llm=True, polymarket_slippage=2.0)

        # 2. Mock OrderBook for the asset
        asset_id = "test_asset_1"
        ob = OrderBook(token_id=asset_id)
        # Bids: 0.50 (10000) -> 5000 USD depth at 0.50
        # Asks: 0.51 (10000) -> 5100 USD depth at 0.51
        ob.update("buy", 0.50, 10000)
        ob.update("sell", 0.51, 10000)
        self.service._order_books[asset_id] = ob
        
        # 3. Mock Market Info
        self.service._market_cache[asset_id] = {
            "question": "Will BTC reach 100k?",
            "outcome": "Yes",
            "slug": "btc-reach-100k",
            "event_slug": "crypto-predictions"
        }

        # 4. Simulate a Whale Trade
        # Trade: BUY 20000 shares at 0.51 = 10200 USD
        # Threshold (2% slippage) is max(5000, 5100) = 5100.
        # Trade 10200 > 5100 and > user threshold 5000.
        trade = {
            "event_type": "trade",
            "asset_id": asset_id,
            "price": 0.51,
            "size": 20000,
            "side": "BUY",
            "condition_id": "cond_1"
        }

        mock_generate_summary.return_value = "AI: This is a significant BTC bet."

        # Execute
        await self.service._handle_trade(trade)

        # 5. Verify Notifications
        self.mock_bot.queue_dm.assert_called_once()
        args, kwargs = self.mock_bot.queue_dm.call_args
        target_uid = args[0]
        embed = kwargs["embed"]

        self.assertEqual(target_uid, user_id)
        self.assertIn("BTC reach 100k?", embed.description)
        self.assertIn("AI: This is a significant BTC bet.", embed.description)
        self.assertIn("$10,200.00", embed.description)

    @patch("services.polymarket_service.httpx.AsyncClient")
    async def test_fetch_active_assets(self, mock_client_class):
        # Mock Gamma API response
        mock_client = AsyncMock()
        mock_client_class.return_value.__aenter__.return_value = mock_client
        
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = [
            {
                "question": "Test Market",
                "clobTokenIds": '["token1", "token2"]',
                "outcomes": '["Yes", "No"]',
                "outcomePrices": '[0.5, 0.5]',
                "description": "Test Desc",
                "slug": "test-market",
                "conditionId": "cond1"
            }
        ]
        mock_client.get.return_value = mock_response

        asset_ids = await self.service._fetch_all_active_asset_ids()
        
        self.assertIn("token1", asset_ids)
        self.assertIn("token2", asset_ids)
        self.assertEqual(self.service._market_cache["token1"]["question"], "Test Market")

if __name__ == "__main__":
    unittest.main()
