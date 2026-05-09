import unittest
import sqlite3
import json
import asyncio
from pathlib import Path
import tempfile
from unittest.mock import MagicMock, patch

from database.core import run_migrations
from services.asset_manager import AssetManager
from models.asset import ContextType, Asset, TradeMetadata, HoldingMetadata
from database.user_settings import get_full_user_context, upsert_user_config

class TestUnifiedAssetLifecycle(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        # Setup logging to see migration progress
        import logging
        logging.basicConfig(level=logging.INFO)
        
        # Setup isolated database
        self._tmpdir = tempfile.TemporaryDirectory()
        self.db_path = str(Path(self._tmpdir.name) / "test_assets.db")

        # Patch DB_NAME in all modules that use it at top level
        self._patchers = [
            patch("database.core.DB_NAME", self.db_path),
            patch("database.user_settings.DB_NAME", self.db_path),
            patch("services.asset_manager.DB_NAME", self.db_path),
            patch("config.DB_NAME", self.db_path),
        ]
        for p in self._patchers:
            p.start()

        run_migrations()
        self.manager = AssetManager(self.db_path)
        self.user_id = 999

    def tearDown(self):
        for p in reversed(self._patchers):
            p.stop()
        self._tmpdir.cleanup()

    async def test_asset_lifecycle_flow(self):
        # 1. Add WATCH
        asset_watch = Asset(
            user_id=self.user_id,
            symbol="TSLA",
            context_type=ContextType.WATCH,
            metadata={"use_llm": True}
        )
        self.assertTrue(self.manager.add_asset(asset_watch))
        
        # Verify WATCH
        assets = self.manager.get_assets(self.user_id, ContextType.WATCH)
        self.assertEqual(len(assets), 1)
        self.assertEqual(assets[0].symbol, "TSLA")
        
        # 2. Promote to TRADE
        trade_details = {
            "opt_type": "put",
            "strike": 150.0,
            "expiry": "2026-06-19",
            "entry_price": 5.0,
            "quantity": -2, # STO 2 contracts
            "weighted_delta": -45.0, # Pre-calculated mock
            "theta": 12.0
        }
        self.assertTrue(self.manager.promote_to_trade(self.user_id, "TSLA", trade_details))
        
        # Verify TRADE
        trades = self.manager.get_assets(self.user_id, ContextType.TRADE)
        self.assertEqual(len(trades), 1)
        self.assertEqual(trades[0].context_type, ContextType.TRADE)
        meta = TradeMetadata(**trades[0].metadata)
        self.assertEqual(meta.quantity, -2)
        
        # 3. Risk Aggregation Verification
        upsert_user_config(self.user_id, portfolio_value=100000.0)
        ctx = get_full_user_context(self.user_id)
        # Delta should be -45.0 from trade
        self.assertEqual(ctx.total_weighted_delta, -45.0)
        
        # 4. Settle to HOLDING
        # Suppose we got assigned at 150.0
        trade_id = trades[0].id
        self.assertTrue(self.manager.settle_to_holding(self.user_id, trade_id, 150.0))
        
        # Verify HOLDING
        holdings = self.manager.get_assets(self.user_id, ContextType.HOLDING)
        self.assertEqual(len(holdings), 1)
        self.assertEqual(holdings[0].context_type, ContextType.HOLDING)
        h_meta = HoldingMetadata(**holdings[0].metadata)
        self.assertEqual(h_meta.quantity, -200.0) # -2 * 100
        self.assertEqual(h_meta.avg_cost, 150.0)

    @patch("services.market_data_service.get_history_df")
    @patch("services.market_data_service.get_quote")
    async def test_nro_aggregation_with_holdings(self, mock_quote, mock_hist):
        # Setup user config
        upsert_user_config(self.user_id, portfolio_value=100000.0)
        
        # Setup mock data for Greeks refresh
        mock_quote.return_value = {"c": 160.0} # TSLA price
        
        # Add a HOLDING
        h_asset = Asset(
            user_id=self.user_id,
            symbol="TSLA",
            context_type=ContextType.HOLDING,
            metadata={"quantity": 100, "avg_cost": 150.0, "weighted_delta": 100.0} # Mock delta
        )
        self.manager.add_asset(h_asset)
        
        # Add a TRADE
        t_asset = Asset(
            user_id=self.user_id,
            symbol="AAPL",
            context_type=ContextType.TRADE,
            metadata={
                "opt_type": "call", "strike": 200, "expiry": "2026-01-01", 
                "entry_price": 10, "quantity": 1, "weighted_delta": 50.0
            }
        )
        self.manager.add_asset(t_asset)
        
        # Aggregation check
        ctx = get_full_user_context(self.user_id)
        # Total Delta = 100.0 (Holding) + 50.0 (Trade) = 150.0
        self.assertEqual(ctx.total_weighted_delta, 150.0)

if __name__ == "__main__":
    unittest.main()
