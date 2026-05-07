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
import tempfile
from pathlib import Path

# Use a local test DB in a temp dir
temp_dir = tempfile.TemporaryDirectory()
TEST_DB = str(Path(temp_dir.name) / "test_nexus_data_pro.db")

import database.core
import database.user_settings
import database.portfolio
import database.watchlist
import database.virtual_trading
import database.financials
import database.cache

def apply_db_patch():
    database.core.DB_NAME = TEST_DB
    database.user_settings.DB_NAME = TEST_DB
    database.portfolio.DB_NAME = TEST_DB
    database.watchlist.DB_NAME = TEST_DB
    database.virtual_trading.DB_NAME = TEST_DB
    database.financials.DB_NAME = TEST_DB
    database.cache.DB_NAME = TEST_DB
    config.DB_NAME = TEST_DB

apply_db_patch()

from database.user_settings import upsert_user_config, get_full_user_context
from database.core import run_migrations, init_db

class TestProInvestorIntegration(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        # Initialize test DB and run migrations
        init_db()
        run_migrations()

    @classmethod
    def tearDownClass(cls):
        temp_dir.cleanup()

    def setUp(self):
        self.user_id = 99999999

    def test_user_settings_pro_fields(self):
        # 1. Update with pro fields
        success = upsert_user_config(
            self.user_id,
            monthly_expense=5000.0,
            tax_reserve_rate=0.25,
            cash_reserve=50000.0
        )
        self.assertTrue(success)
        
        # 2. Retrieve and verify
        ctx = get_full_user_context(self.user_id)
        self.assertEqual(ctx.monthly_expense, 5000.0)
        self.assertEqual(ctx.tax_reserve_rate, 0.25)
        self.assertEqual(ctx.cash_reserve, 50000.0)
        
        # 3. Test validation caps
        upsert_user_config(self.user_id, tax_reserve_rate=1.5) # Should cap at 1.0
        ctx = get_full_user_context(self.user_id)
        self.assertEqual(ctx.tax_reserve_rate, 1.0)
        
        upsert_user_config(self.user_id, monthly_expense=-100.0) # Should cap at 0.0
        ctx = get_full_user_context(self.user_id)
        self.assertEqual(ctx.monthly_expense, 0.0)
        
        upsert_user_config(self.user_id, cash_reserve=-1000.0) # Should cap at 0.0
        ctx = get_full_user_context(self.user_id)
        self.assertEqual(ctx.cash_reserve, 0.0)

if __name__ == '__main__':
    unittest.main()
