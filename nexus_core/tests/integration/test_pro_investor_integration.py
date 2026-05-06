import unittest
import sys
from unittest.mock import MagicMock

# Mock py_vollib
sys.modules["py_vollib"] = MagicMock()
sys.modules["py_vollib.black_scholes_merton"] = MagicMock()
sys.modules["py_vollib.black_scholes_merton.greeks"] = MagicMock()
sys.modules["py_vollib.black_scholes_merton.greeks.analytical"] = MagicMock()

import sqlite3
import os
import config

# Use a local test DB
TEST_DB = "test_nexus_data.db"
config.DB_NAME = TEST_DB

# Ensure all database modules use the test DB
import database.core
import database.user_settings
import database.portfolio
import database.watchlist
import database.virtual_trading
import database.financials
import database.cache

database.core.DB_NAME = TEST_DB
database.user_settings.DB_NAME = TEST_DB
database.portfolio.DB_NAME = TEST_DB
database.watchlist.DB_NAME = TEST_DB
database.virtual_trading.DB_NAME = TEST_DB
database.financials.DB_NAME = TEST_DB
database.cache.DB_NAME = TEST_DB

from database.user_settings import upsert_user_config, get_full_user_context
from database.core import run_migrations, init_db

class TestProInvestorIntegration(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        # Initialize test DB and run migrations
        if os.path.exists(TEST_DB):
            os.remove(TEST_DB)
        init_db()
        run_migrations()

    @classmethod
    def tearDownClass(cls):
        if os.path.exists(TEST_DB):
            os.remove(TEST_DB)

    def setUp(self):
        self.user_id = 99999999

    def test_user_settings_pro_fields(self):
        # 1. Update with pro fields
        success = upsert_user_config(
            self.user_id,
            is_professional_mode=True,
            monthly_expense=5000.0,
            tax_reserve_rate=0.25
        )
        self.assertTrue(success)
        
        # 2. Retrieve and verify
        ctx = get_full_user_context(self.user_id)
        self.assertTrue(ctx.is_professional_mode)
        self.assertEqual(ctx.monthly_expense, 5000.0)
        self.assertEqual(ctx.tax_reserve_rate, 0.25)
        
        # 3. Test validation caps
        upsert_user_config(self.user_id, tax_reserve_rate=1.5) # Should cap at 1.0
        ctx = get_full_user_context(self.user_id)
        self.assertEqual(ctx.tax_reserve_rate, 1.0)
        
        upsert_user_config(self.user_id, monthly_expense=-100.0) # Should cap at 0.0
        ctx = get_full_user_context(self.user_id)
        self.assertEqual(ctx.monthly_expense, 0.0)

if __name__ == '__main__':
    unittest.main()
