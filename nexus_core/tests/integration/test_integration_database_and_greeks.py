import asyncio
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import pandas as pd

from database.core import run_migrations
from database.portfolio import add_portfolio_record
from database.user_settings import get_full_user_context, upsert_user_config
from database.virtual_trading import add_virtual_trade
from market_analysis.portfolio import refresh_portfolio_greeks


class DbIsolatedTestCase(unittest.TestCase):
    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self.db_path = str(Path(self._tmpdir.name) / "integration_test.db")

        self._db_patchers = [
            patch("database.core.DB_NAME", self.db_path),
            patch("database.portfolio.DB_NAME", self.db_path),
            patch("database.user_settings.DB_NAME", self.db_path),
            patch("database.virtual_trading.DB_NAME", self.db_path),
        ]
        for patcher in self._db_patchers:
            patcher.start()

        run_migrations()

    def tearDown(self):
        for patcher in reversed(self._db_patchers):
            patcher.stop()
        self._tmpdir.cleanup()


class TestUserContextAggregation(DbIsolatedTestCase):
    def test_context_aggregates_real_and_virtual_greeks(self):
        upsert_user_config(1001, capital=50000.0, risk_limit_pct=99.0)
        upsert_user_config(1002, capital=120000.0, risk_limit_pct=10.0)

        add_portfolio_record(
            user_id=1001,
            symbol="TSLA",
            opt_type="call",
            strike=250.0,
            expiry="2099-04-17",
            entry_price=2.0,
            quantity=1,
            stock_cost=0.0,
            weighted_delta=10.0,
            theta=-2.0,
            gamma=0.2,
        )
        add_portfolio_record(
            user_id=1002,
            symbol="AAPL",
            opt_type="put",
            strike=170.0,
            expiry="2099-04-17",
            entry_price=1.0,
            quantity=-1,
            stock_cost=0.0,
            weighted_delta=-50.0,
            theta=-9.0,
            gamma=2.0,
        )

        add_virtual_trade(
            user_id=1001,
            symbol="TSLA",
            opt_type="call",
            strike=260.0,
            expiry="2099-05-17",
            entry_price=3.0,
            quantity=1,
            weighted_delta=5.0,
            theta=-1.0,
            gamma=0.1,
        )

        ctx = get_full_user_context(1001)

        self.assertEqual(ctx.user_id, 1001)
        self.assertEqual(ctx.capital, 50000.0)
        self.assertEqual(ctx.risk_limit_base, 50.0)
        self.assertAlmostEqual(ctx.total_weighted_delta, 15.0)
        self.assertAlmostEqual(ctx.total_theta, -3.0)
        self.assertAlmostEqual(ctx.total_gamma, 0.3)


class TestRefreshPortfolioGreeks(DbIsolatedTestCase):
    def test_refresh_writes_portfolio_and_virtual_trade_greeks(self):
        add_portfolio_record(
            user_id=1,
            symbol="TSLA",
            opt_type="call",
            strike=250.0,
            expiry="2099-04-17",
            entry_price=2.5,
            quantity=2,
            stock_cost=0.0,
        )
        add_virtual_trade(
            user_id=1,
            symbol="TSLA",
            opt_type="call",
            strike=250.0,
            expiry="2099-04-17",
            entry_price=2.0,
            quantity=-1,
        )

        async def fake_history(symbol, _period):
            if symbol == "SPY":
                return pd.DataFrame({"Close": [500.0, 500.0, 500.0]})
            return pd.DataFrame({"Close": [150.0, 151.0, 150.0]})

        async def fake_quote(_symbol):
            return {"c": 150.0}

        async def fake_dividend(_symbol):
            return 0.02

        with patch("market_analysis.portfolio.market_data_service.get_history_df", side_effect=fake_history), patch(
            "market_analysis.portfolio.market_data_service.get_quote", side_effect=fake_quote
        ), patch(
            "market_analysis.portfolio.market_data_service.get_dividend_yield", side_effect=fake_dividend
        ), patch(
            "market_analysis.portfolio.get_option_chain_mid_iv", return_value=(2.0, 0.25)
        ), patch(
            "market_analysis.portfolio.calculate_greeks",
            return_value={"delta": 0.5, "theta": -0.01, "gamma": 0.02},
        ), patch("market_analysis.portfolio.calculate_beta", return_value=1.2):
            asyncio.run(refresh_portfolio_greeks(user_id=1))

        with sqlite3.connect(self.db_path) as conn:
            row = conn.execute(
                "SELECT weighted_delta, theta, gamma FROM portfolio WHERE user_id = 1"
            ).fetchone()
            self.assertIsNotNone(row)
            self.assertAlmostEqual(row[0], 36.0, places=4)
            self.assertAlmostEqual(row[1], -2.0, places=4)
            self.assertAlmostEqual(row[2], 0.5184, places=4)

            vrow = conn.execute(
                "SELECT weighted_delta, theta, gamma FROM virtual_trades WHERE user_id = 1"
            ).fetchone()
            self.assertIsNotNone(vrow)
            self.assertAlmostEqual(vrow[0], -18.0, places=4)
            self.assertAlmostEqual(vrow[1], 1.0, places=4)
            self.assertAlmostEqual(vrow[2], -0.2592, places=4)
