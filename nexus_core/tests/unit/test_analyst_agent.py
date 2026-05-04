import unittest
import sqlite3
import asyncio
from unittest.mock import MagicMock, patch, AsyncMock

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

    @patch('cogs.analyst_agent.get_reddit_context')
    @patch('cogs.analyst_agent.fetch_recent_news')
    @patch('cogs.analyst_agent.generate_analyst_report')
    @patch('cogs.analyst_agent.get_earnings_calendar')
    @patch('cogs.analyst_agent.get_history_df')
    @patch('cogs.analyst_agent.analyze_hedge_performance')
    @patch('cogs.analyst_agent.get_all_watchlist')
    @patch.object(AnalystAgent, '_fetch_macro_data')
    async def test_analyst_agent_reports(self, mock_fetch, mock_watchlist, mock_hedge, mock_history, mock_earnings, mock_llm, mock_news, mock_reddit):
        # Mocking yfinance fetch
        mock_fetch.return_value = (18.5, 105.2, 4.2)
        
        # Mock dependencies
        mock_watchlist.return_value = [(999, 'AAPL'), (999, 'TSLA')]
        mock_earnings.return_value = [{"date": "2026-05-01", "time": "AMC"}]
        mock_news.return_value = "Mocked News Headline"
        mock_reddit.return_value = '{"score": 8.5, "sentiment": "Bullish"}'
        
        import pandas as pd
        mock_df = pd.DataFrame({'Close': [100.0, 105.0], 'Volume': [1000, 2000]})
        mock_history.return_value = mock_df
        
        mock_hedge.return_value = {
            "net_pnl": 100.0,
            "alpha_contribution": 80.0,
            "hedge_contribution": 20.0,
            "hedge_ratio": 0.5,
            "effectiveness": 0.8
        }
        
        # Simulate LLM behavior
        async def fake_llm(report_type, raw_data):
            return f"**{report_type}**\nMocked NLP Analysis"
        mock_llm.side_effect = fake_llm

        bot_mock = MagicMock()
        bot_mock.wait_until_ready = AsyncMock()
        
        agent = AnalystAgent(bot_mock)
        # Stop background tasks
        agent.pre_market_loop.cancel()
        agent.intra_day_loop.cancel()
        agent.post_market_loop.cancel()

        # Test macro scan report
        report1 = await agent.run_macro_scan()
        self.assertIn("巨觀環境與隔夜市場掃描", report1)
        self.assertIn("105.2", report1) # Checks DXY

        # Test premarket earnings
        report2 = await agent.run_premarket_earnings()
        self.assertIn("盤前財報與估值調整", report2)
        self.assertIn("Mocked NLP Analysis", report2)

        # Test next-day strategy
        report3 = await agent.run_next_day_strategy()
        self.assertIn("次日策略制定", report3)
        self.assertIn("18.5", report3) # Checks VIX

        # Test deep research
        report4 = await agent.run_deep_research()
        self.assertIn("深度研究與特定板塊分析", report4)
        self.assertIn("Mocked NLP Analysis", report4)

        # Test portfolio hedging
        report5 = await agent.run_portfolio_hedging()
        self.assertIn("投資組合再平衡與避險策略", report5)
        self.assertIn("Mocked NLP Analysis", report5)

        # Test postmarket summary
        report6 = await agent.run_postmarket_summary()
        self.assertIn("盤後交易與每日總結", report6)
        self.assertIn("Mocked NLP Analysis", report6)

        # Test market open liquidity
        report7 = await agent.run_market_open_liquidity()
        self.assertIn("開盤與流動性執行監控", report7)
        self.assertIn("Mocked NLP Analysis", report7)

    @patch.object(AnalystAgent, '_fetch_macro_data')
    async def test_run_macro_scan_alerts(self, mock_fetch):
        bot_mock = MagicMock()
        bot_mock.wait_until_ready = AsyncMock()
        agent = AnalystAgent(bot_mock)
        agent.pre_market_loop.cancel()
        agent.intra_day_loop.cancel()
        agent.post_market_loop.cancel()
        
        # Test 1: All clear (Safe state)
        mock_fetch.return_value = {
            'vix': 15.0, 'vix_change': 0.5,
            'dxy': 100.0,
            'tnx': 4.0, 'tnx_change_bps': 2.0,
            'us2y': 3.9 # spread 0.1
        }
        report = await agent.run_macro_scan()
        self.assertIn("✅ **巨觀狀態：**", report)
        self.assertNotIn("🚨 **風險警示：**", report)

        # Test 2: Inverted Yield Curve (spread < -0.2)
        mock_fetch.return_value = {
            'vix': 15.0, 'vix_change': 0.5,
            'dxy': 100.0,
            'tnx': 4.0, 'tnx_change_bps': 2.0,
            'us2y': 4.3 # spread -0.3
        }
        report = await agent.run_macro_scan()
        self.assertIn("🚨 **風險警示：**", report)
        self.assertIn("殖利率曲線深度倒掛", report)
        
        # Test 3: Steepening (-0.1 <= spread <= 0.2 and tnx_change_bps < 0)
        mock_fetch.return_value = {
            'vix': 15.0, 'vix_change': 0.5,
            'dxy': 100.0,
            'tnx': 4.0, 'tnx_change_bps': -5.0,
            'us2y': 3.9 # spread 0.1
        }
        report = await agent.run_macro_scan()
        self.assertIn("🚨 **風險警示：**", report)
        self.assertIn("殖利率曲線接近解除倒掛", report)
        
        # Test 4: Rate alert (tnx > 4.5 and tnx_change_bps > 8)
        mock_fetch.return_value = {
            'vix': 15.0, 'vix_change': 0.5,
            'dxy': 100.0,
            'tnx': 4.6, 'tnx_change_bps': 10.0,
            'us2y': 4.5 # spread 0.1
        }
        report = await agent.run_macro_scan()
        self.assertIn("10 年期殖利率突破 4.5% 且短期急升", report)
        
        # Test 5: Volatility alert (vix > 20 and vix_change > 2.0)
        mock_fetch.return_value = {
            'vix': 25.0, 'vix_change': 3.0,
            'dxy': 100.0,
            'tnx': 4.0, 'tnx_change_bps': 1.0,
            'us2y': 3.9 
        }
        report = await agent.run_macro_scan()
        self.assertIn("恐慌指數急遽上升", report)
        
        # Test 6: Currency alert (dxy > 105)
        mock_fetch.return_value = {
            'vix': 15.0, 'vix_change': 0.5,
            'dxy': 106.0,
            'tnx': 4.0, 'tnx_change_bps': 1.0,
            'us2y': 3.9 
        }
        report = await agent.run_macro_scan()
        self.assertIn("美元指數處於強勢區間", report)
        
        # Test 7: Formatting checks
        self.assertIn("106.00", report) # dxy:.2f
        self.assertIn("4.00%", report) # tnx:.2f
        self.assertIn("3.90%", report) # us2y:.2f
        self.assertIn("+1.0 bps", report) # tnx_change_bps:+.1f
        self.assertIn("+0.10%", report) # spread:+.2f


class TestAnalystAgentVixNaN(unittest.IsolatedAsyncioTestCase):
    async def test_run_next_day_strategy_handles_nan(self):
        """Verify that AnalystAgent handles NaN VIX gracefully in strategy reports."""
        bot_mock = MagicMock()
        bot_mock.wait_until_ready = AsyncMock()
        agent = AnalystAgent(bot_mock)
        agent.pre_market_loop.cancel()
        agent.intra_day_loop.cancel()
        agent.post_market_loop.cancel()

        # Mock _fetch_macro_data to return NaN
        async def mock_fetch():
            return {'vix': float('nan'), 'vix_change': 0.0, 'dxy': 0.0, 'tnx': 0.0, 'tnx_change_bps': 0.0, 'us2y': 0.0}
        
        with patch.object(AnalystAgent, '_fetch_macro_data', side_effect=mock_fetch):
            report = await agent.run_next_day_strategy()
            # Should display N/A and still map to a tier (Ready)
            self.assertIn("N/A (Using Default)", report)
            self.assertIn("摩拳擦掌 (Ready)", report)

if __name__ == '__main__':
    unittest.main()
