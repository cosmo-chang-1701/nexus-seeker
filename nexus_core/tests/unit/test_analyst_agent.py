import unittest
import sqlite3
import asyncio
from unittest.mock import MagicMock, patch

import database
from database import core as db_core
from database import user_settings

from cogs.analyst_agent import AnalystAgent

import tempfile
import os

class TestAnalystAgent(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        # Use a temporary file for the DB to allow multiple connections
        self.temp_db_fd, self.temp_db_path = tempfile.mkstemp()
        
        self.original_db = user_settings.DB_NAME
        user_settings.DB_NAME = self.temp_db_path
        db_core.DB_NAME = self.temp_db_path
        database.DB_NAME = self.temp_db_path
        
        # Initialize DB with migrations (includes v017)
        db_core.init_db()

    def tearDown(self):
        # Restore DB name
        user_settings.DB_NAME = self.original_db
        db_core.DB_NAME = self.original_db
        database.DB_NAME = self.original_db
        
        # Cleanup temp DB
        os.close(self.temp_db_fd)
        os.remove(self.temp_db_path)

    def test_user_settings_analyst_agent_flag(self):
        user_id = 999
        
        # 1. Test upsert with enable_analyst_agent=True
        success = user_settings.upsert_user_config(user_id, enable_analyst_agent=True)
        self.assertTrue(success, "Upserting enable_analyst_agent should succeed")
        
        # 2. Test fetching full context
        context = user_settings.get_full_user_context(user_id)
        self.assertTrue(context.enable_analyst_agent, "Context should have enable_analyst_agent=True")
        
        # 3. Test upsert to False
        success = user_settings.upsert_user_config(user_id, enable_analyst_agent=False)
        self.assertTrue(success)
        
        context = user_settings.get_full_user_context(user_id)
        self.assertFalse(context.enable_analyst_agent, "Context should have enable_analyst_agent=False")

    @patch.object(AnalystAgent, '_fetch_macro_data')
    async def test_analyst_agent_reports(self, mock_fetch):
        # Mocking yfinance fetch
        mock_fetch.return_value = (18.5, 105.2, 4.2)

        bot_mock = MagicMock()
        agent = AnalystAgent(bot_mock)
        
        # Stop the background task from actually running in test
        agent.analyst_task.cancel()

        # Test macro scan report
        report1 = await agent.run_macro_scan()
        self.assertIn("Global Macro Environment", report1)
        self.assertIn("105.2", report1) # Checks DXY

        # Test premarket earnings
        report2 = await agent.run_premarket_earnings()
        self.assertIn("Pre-Market Earnings Reports", report2)

        # Test next-day strategy
        report3 = await agent.run_next_day_strategy()
        self.assertIn("Next-Day Strategy Formulation", report3)
        self.assertIn("18.5", report3) # Checks VIX

if __name__ == '__main__':
    unittest.main()
