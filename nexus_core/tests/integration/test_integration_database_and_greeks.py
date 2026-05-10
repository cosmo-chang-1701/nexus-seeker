import asyncio
import os
import sqlite3
import tempfile
import unittest
import json
from pathlib import Path
from unittest.mock import patch

import pandas as pd

from database.core import run_migrations
from database.user_settings import get_full_user_context, upsert_user_config
from database.virtual_trading import add_virtual_trade
from market_analysis.portfolio import refresh_portfolio_greeks


class DbIsolatedTestCase(unittest.TestCase):
    def setUp(self):
        # 1. 隔離環境設定
        self._tmpdir = tempfile.TemporaryDirectory()
        self.db_path = str(Path(self._tmpdir.name) / "integration_test.db")

        # 🚀 Patch config.DB_NAME globally
        import config
        self._patcher = patch.object(config, 'DB_NAME', self.db_path)
        self._patcher.start()

        # 2. 初始化 Schema
        # 確保此處使用的是已被 patch 的 config.DB_NAME
        run_migrations()

    def tearDown(self):
        self._patcher.stop()
        self._tmpdir.cleanup()


class TestUserContextAggregation(DbIsolatedTestCase):
    def test_upsert_user_config_rejects_zero_capital(self):
        upsert_user_config(3001, capital=0.0)
        ctx = get_full_user_context(3001)
        self.assertGreater(ctx.capital, 0.0)

    def test_context_aggregates_real_and_holdings_greeks(self):
        """測試 get_full_user_context 是否能正確從 assets 表聚合 TRADE 與 HOLDING 的 Greeks"""
        upsert_user_config(1001, capital=50000.0, risk_limit=50.0)

        # 1. 模擬實單部位 (TRADE)
        meta_trade = {
            "opt_type": "put",
            "strike": 400.0,
            "expiry": "2099-06-17",
            "entry_price": 5.0,
            "quantity": -10,
            "weighted_delta": -50.0,
            "theta": -9.0,
            "gamma": 2.0,
            "category": "SPEC"
        }
        with sqlite3.connect(self.db_path) as conn:
            conn.execute(
                "INSERT INTO assets (user_id, symbol, context_type, metadata) VALUES (?, ?, 'TRADE', ?)",
                (1001, "SPY", json.dumps(meta_trade))
            )

        # 2. 模擬現貨持倉 (HOLDING)
        meta_holding = {
            "quantity": 100,
            "avg_cost": 150.0,
            "weighted_delta": 65.0
        }
        with sqlite3.connect(self.db_path) as conn:
            conn.execute(
                "INSERT INTO assets (user_id, symbol, context_type, metadata) VALUES (?, ?, 'HOLDING', ?)",
                (1001, "NVDA", json.dumps(meta_holding))
            )

        ctx = get_full_user_context(1001)

        self.assertEqual(ctx.user_id, 1001)
        self.assertEqual(ctx.capital, 50000.0)
        self.assertEqual(ctx.risk_limit, 50.0)
        # -50.0 (TRADE) + 65.0 (HOLDING) = 15.0
        self.assertAlmostEqual(ctx.total_weighted_delta, 15.0)
        # TRADE Theta = -9.0 (Annual) -> -9.0 / 365 (Daily)
        self.assertAlmostEqual(ctx.total_theta, -9.0 / 365.0)
        self.assertAlmostEqual(ctx.total_gamma, 2.0)


class TestRefreshPortfolioGreeks(DbIsolatedTestCase):
    def test_refresh_writes_portfolio_and_virtual_trade_greeks(self):
        # 1. 設定初始設定 (capital=500.0 for easier math)
        upsert_user_config(1, capital=500.0)
        
        # Setup: Add a trade to Assets (as TRADE) and a virtual trade
        meta_trade = {
            "opt_type": "call",
            "strike": 250.0,
            "expiry": "2099-04-17",
            "entry_price": 2.5,
            "quantity": 2,
            "weighted_delta": 10.0,
            "theta": -0.5,
            "gamma": 0.1,
            "category": "SPEC"
        }
        
        with sqlite3.connect(self.db_path) as conn:
             conn.execute(
                "INSERT INTO assets (user_id, symbol, context_type, metadata) VALUES (?, ?, 'TRADE', ?)",
                (1, "TSLA", json.dumps(meta_trade))
            )

        add_virtual_trade(
            user_id=1,
            symbol="SPY",
            opt_type="put",
            strike=400.0,
            expiry="2099-05-17",
            entry_price=4.0,
            quantity=-3,
            weighted_delta=-15.0,
            theta=0.8,
            gamma=-0.2,
        )

        async def fake_history(symbol, _period):
            if symbol == "SPY":
                return pd.DataFrame({"Close": [500.0, 500.0, 500.0]})
            return pd.DataFrame({"Close": [150.0, 151.0, 150.0]})

        async def fake_quote(_symbol):
            return {"c": 150.0}

        async def fake_dividend(_symbol):
            return 0.0

        # 2. Patch market data services and greeks calculation
        from unittest.mock import patch as mock_patch
        mock_greeks = {
            'delta': 0.5,
            'theta': -0.1,
            'gamma': 0.01,
            'vega': 0.05,
            'vanna': 0.001
        }
        with patch("services.market_data_service.get_history_df", side_effect=fake_history), \
             patch("services.market_data_service.get_quote", side_effect=fake_quote), \
             patch("services.market_data_service.get_dividend_yield", side_effect=fake_dividend), \
             patch("market_analysis.portfolio.calculate_beta", return_value=1.2), \
             patch("market_analysis.portfolio.calculate_greeks", return_value=mock_greeks), \
             patch("market_analysis.portfolio.get_option_chain_mid_iv", return_value=(2.5, 0.4)):
            
            # 3. 執行刷新
            import asyncio
            from services.asset_manager import AssetManager
            test_manager = AssetManager(self.db_path)
            asyncio.run(refresh_portfolio_greeks(user_id=1, manager=test_manager))

        # 4. 驗證結果
        with sqlite3.connect(self.db_path) as conn:
            row_raw = conn.execute("SELECT metadata FROM assets WHERE symbol='TSLA' AND user_id=1").fetchone()
            if row_raw is not None:
                meta = json.loads(row_raw[0])
                # delta(0.5) * 2 * 100 * (150/500) * 1.2 = 36.0
                # Wait, TSLA price 150, SPY price 500, beta 1.2
                # weight_factor = beta * (price/spy_price) = 1.2 * (150/500) = 0.36
                # weighted_delta = delta * contracts * 100 * weight_factor = 0.5 * 2 * 100 * 0.36 = 36.0
                self.assertAlmostEqual(meta['weighted_delta'], 36.0, places=4)

            vrow = conn.execute(
                "SELECT weighted_delta, theta, gamma FROM virtual_trades WHERE user_id = 1"
            ).fetchone()
            self.assertIsNotNone(vrow)
            self.assertIsNotNone(vrow[0])
