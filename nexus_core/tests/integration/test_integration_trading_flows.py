import asyncio
import datetime as dt
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import pandas as pd

from database.core import run_migrations
from database.virtual_trading import get_all_virtual_trades
from market_analysis.ghost_trader import GhostTrader
from market_analysis.portfolio import check_portfolio_status_logic


class DbIsolatedTestCase(unittest.TestCase):
    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self.db_path = str(Path(self._tmpdir.name) / "integration_test.db")

        self._db_patchers = [
            patch("database.core.DB_NAME", self.db_path),
            patch("database.virtual_trading.DB_NAME", self.db_path),
            patch("database.portfolio.DB_NAME", self.db_path),
        ]
        for patcher in self._db_patchers:
            patcher.start()

        run_migrations()

    def tearDown(self):
        for patcher in reversed(self._db_patchers):
            patcher.stop()
        self._tmpdir.cleanup()


class TestGhostTraderFlow(DbIsolatedTestCase):
    def test_entry_and_auto_exit_lifecycle(self):
        expiry = (dt.date.today() + dt.timedelta(days=40)).strftime("%Y-%m-%d")
        trader = GhostTrader()

        with patch.object(
            trader,
            "get_option_mid_price",
            side_effect=[(3.5, 0.25), (7.2, 0.25)],
        ), patch("market_analysis.ghost_trader.market_data_service.get_ema", return_value=None), patch(
            "market_analysis.ghost_trader.market_data_service.get_quote", return_value=None
        ):
            trade_id = asyncio.run(
                trader.record_virtual_entry(
                    user_id=1,
                    symbol="TSLA",
                    opt_type="call",
                    strike=250.0,
                    expiry=expiry,
                    quantity=1,
                )
            )
            self.assertIsNotNone(trade_id)

            asyncio.run(trader.manage_virtual_positions())

        trades = get_all_virtual_trades(1)
        self.assertEqual(len(trades), 1)
        trade = trades[0]

        self.assertEqual(trade["status"], "CLOSED")
        self.assertAlmostEqual(trade["entry_price"], 3.535, places=3)
        self.assertAlmostEqual(trade["exit_price"], 7.128, places=3)
        self.assertAlmostEqual(trade["pnl"], 359.3, places=1)


class TestPortfolioStatusFlow(unittest.TestCase):
    def test_orchestrator_generates_position_macro_and_correlation_sections(self):
        portfolio_rows = [
            ("TSLA", "call", 250.0, "2099-04-17", 2.0, -1, 0.0),
            ("AAPL", "put", 180.0, "2099-04-17", 3.0, 1, 0.0),
        ]

        async def fake_history(_symbol, _period):
            idx = pd.date_range("2025-01-01", periods=90, freq="D")
            return pd.DataFrame({"Close": [500.0 + i for i in range(90)]}, index=idx)

        async def fake_quote(symbol):
            return {"c": 200.0 if symbol == "TSLA" else 180.0}

        async def fake_is_etf(_symbol):
            return False

        async def fake_dividend(_symbol):
            return 0.01

        calls = pd.DataFrame(
            {
                "strike": [250.0],
                "lastPrice": [1.5],
                "impliedVolatility": [0.3],
            }
        )
        puts = pd.DataFrame(
            {
                "strike": [180.0],
                "lastPrice": [2.4],
                "impliedVolatility": [0.28],
            }
        )

        with patch("market_analysis.portfolio.market_data_service.get_history_df", side_effect=fake_history), patch(
            "market_analysis.portfolio.market_data_service.get_quote", side_effect=fake_quote
        ), patch(
            "market_analysis.portfolio.market_data_service.is_etf", side_effect=fake_is_etf
        ), patch(
            "market_analysis.portfolio.market_data_service.get_dividend_yield", side_effect=fake_dividend
        ), patch(
            "market_analysis.portfolio.get_option_chain", return_value=(calls, puts)
        ), patch(
            "market_analysis.portfolio.calculate_greeks",
            return_value={"delta": 0.25, "theta": -0.01, "gamma": 0.02},
        ), patch(
            "market_analysis.portfolio.calculate_option_margin", return_value=1000.0
        ), patch(
            "market_analysis.portfolio.analyze_sector_correlation_core",
            return_value=[("TSLA", "AAPL", 0.82)],
        ):
            report = asyncio.run(check_portfolio_status_logic(portfolio_rows, user_capital=50000.0))

        report_text = "\n".join(report)
        self.assertIn("TSLA", report_text)
        self.assertIn("AAPL", report_text)
        self.assertIn("SPY Delta", report_text)
        self.assertIn("TSLA` & `AAPL", report_text)
