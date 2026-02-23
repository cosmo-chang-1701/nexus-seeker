"""
test_four_scenarios.py — Mock Data 模擬四種策略情境的端對端測試

四種情境：
  1. STO_PUT  — 超賣收入 (RSI < 35, HV Rank ≥ 30)
  2. STO_CALL — 超買收入 (RSI > 65, HV Rank ≥ 30)
  3. BTO_CALL — 動能突破 (Price > SMA20, 50 ≤ RSI ≤ 65, MACD > 0, HV Rank < 50)
  4. BTO_PUT  — 跌破避險 (Price < SMA20, 35 ≤ RSI ≤ 50, MACD < 0, HV Rank < 50)

每個測試 patch analyze_symbol 管線中的所有子函式，
確保整條管線（技術指標 → 策略訊號 → MMM → 期限結構 → 合約篩選 → 偏態 → 風險/流動性 → 倉位計算）全部走通，
並將結果傳入 create_scan_embed 驗證 Discord Embed 輸出。
"""

import unittest
from unittest.mock import MagicMock, patch
import sys
from types import ModuleType

# --- MOCK DEPENDENCIES BEFORE IMPORTING STRATEGY ---

# (Removed numpy/pandas mocks to allow real pandas to load without crashing)

# Mock yfinance
mock_yf = MagicMock()
sys.modules["yfinance"] = mock_yf

# Mock pandas_ta
mock_pandas_ta = MagicMock()
sys.modules.setdefault("pandas_ta", mock_pandas_ta)

# Mock py_vollib and submodules
mock_vollib = MagicMock()
sys.modules["py_vollib"] = mock_vollib
sys.modules["py_vollib.black_scholes"] = mock_vollib
sys.modules["py_vollib.black_scholes.greeks"] = mock_vollib
sys.modules["py_vollib.black_scholes.greeks.analytical"] = mock_vollib
sys.modules["py_vollib.black_scholes_merton"] = mock_vollib
sys.modules["py_vollib.black_scholes_merton.greeks"] = mock_vollib
sys.modules["py_vollib.black_scholes_merton.greeks.analytical"] = mock_vollib

# Mock config
mock_config = ModuleType("config")
mock_config.TARGET_DELTAS = {
    "STO_PUT": -0.16,
    "STO_CALL": 0.16,
    "BTO_CALL": 0.50,
    "BTO_PUT": -0.50,
}
mock_config.RISK_FREE_RATE = 0.042
mock_config.DISCORD_TOKEN = "mock_token"
mock_config.TARGET_CHANNEL_ID = 0
mock_config.LOG_LEVEL = "WARNING"
mock_config.DB_NAME = ":memory:"
sys.modules["config"] = mock_config

# Mock discord (需要支援 Embed 與 Color)
import discord
# discord 模組真實 import；若不可用再 mock
try:
    from discord import Embed, Color
    DISCORD_AVAILABLE = True
except ImportError:
    DISCORD_AVAILABLE = False

# Now import strategy and embed_builder
import os
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))
from market_analysis import strategy
from cogs.embed_builder import create_scan_embed


# ====================================================================
# 共用 Helper：建構 Mock 合約物件
# ====================================================================
def _make_mock_contract(strike, bid, ask, bs_delta, iv):
    """建構一個 MagicMock 合約，支援 best_contract['key'] 取值"""
    contract = MagicMock()
    data = {
        'strike': strike,
        'bid': bid,
        'ask': ask,
        'bs_delta': bs_delta,
        'impliedVolatility': iv,
    }
    contract.__getitem__ = MagicMock(side_effect=lambda k: data[k])
    return contract


def _assert_embed_valid(test_case, embed, expected_strategy, expected_symbol):
    """驗證 Discord Embed 的基本結構正確性"""
    test_case.assertIsInstance(embed, discord.Embed)
    # Title 應包含策略名稱和標的代號
    test_case.assertIn(expected_symbol, embed.title)
    # 應有多個 field
    test_case.assertTrue(len(embed.fields) >= 6)
    test_case.assertEqual(embed.fields[0].name, "🏷️ 標的現價⠀⠀⠀⠀")

    # 驗證必要欄位存在 (這些是 create_scan_embed 中的常數)
    aroc_fields = [f for f in embed.fields if "AROC" in f.name]
    test_case.assertTrue(len(aroc_fields) > 0, "Embed 應包含 AROC 欄位")
    # 應有 Delta / IV field
    delta_fields = [f for f in embed.fields if "Delta" in f.name]
    test_case.assertTrue(len(delta_fields) > 0, "Embed 應包含 Delta 欄位")


# ====================================================================
# 四種情境端對端測試 + create_scan_embed 驗證
# ====================================================================
class TestFourScenarios(unittest.TestCase):
    """
    每個測試 patch analyze_symbol 內部呼叫的所有子函式，
    注入預先準備的 Mock Data，確認回傳結果符合預期策略，
    並將結果傳入 create_scan_embed 驗證 Discord Embed 輸出。
    """

    # ==============================
    # 情境 1: STO_PUT — 超賣收入
    # ==============================
    @patch('market_analysis.strategy._calculate_technical_indicators')
    @patch('market_analysis.strategy._determine_strategy_signal')
    @patch('market_analysis.strategy._calculate_mmm')
    @patch('market_analysis.strategy._calculate_term_structure')
    @patch('market_analysis.strategy._find_target_expiry')
    @patch('market_analysis.strategy._get_best_contract_data')
    @patch('market_analysis.strategy._calculate_vertical_skew')
    @patch('market_analysis.strategy._validate_risk_and_liquidity')
    @patch('market_analysis.strategy._calculate_sizing')
    @patch('market_analysis.strategy.yf.Ticker')
    def test_scenario_sto_put(self, mock_ticker_cls, mock_sizing, mock_validate,
                               mock_skew, mock_contract, mock_expiry, mock_ts,
                               mock_mmm, mock_signal, mock_indicators):
        """
        STO_PUT 情境：RSI=30 (超賣), HV Rank=40 (高波動)
        預期：策略為 STO_PUT，賣 Put δ≈−0.20，DTE 30–45
        最終驗證 create_scan_embed 產出合法的 Discord Embed
        """
        # Ticker
        mock_ticker = MagicMock()
        mock_ticker_cls.return_value = mock_ticker
        mock_ticker.history.return_value = MagicMock()
        mock_ticker.options = ["2026-03-20", "2026-04-17"]

        # 1. 技術指標 — 超賣
        mock_indicators.return_value = {
            'price': 150.0, 'rsi': 30.0, 'sma20': 160.0,
            'hv_current': 0.25, 'hv_rank': 40.0, 'macd_hist': -1.5,
        }

        # 2. 策略訊號 — STO_PUT
        mock_signal.return_value = ("STO_PUT", "put", -0.20, 30, 45)

        # 3. MMM — 無財報風險
        mock_mmm.return_value = (0.0, 0.0, 0.0, -1)

        # 4. 期限結構 — 正常
        mock_ts.return_value = (0.98, "🌊 正常 (Contango)")

        # 5. 到期日
        mock_expiry.return_value = ("2026-03-20", 35)

        # 6. 最佳合約
        best = _make_mock_contract(strike=140.0, bid=2.50, ask=2.70, bs_delta=-0.18, iv=0.30)
        mock_contract.return_value = (best, MagicMock())

        # 7. 垂直偏態 — 中性
        mock_skew.return_value = (1.05, "⚖️ 中性 (Neutral)")

        # 8. 風險/流動性 — 全通過
        mock_validate.return_value = {
            'bid': 2.50, 'ask': 2.70, 'spread': 0.20, 'spread_ratio': 7.7,
            'vrp': 0.05, 'expected_move': 12.0, 'em_lower': 138.0, 'em_upper': 162.0,
            'mid_price': 2.60, 'suggested_hedge_strike': None,
            'liq_status': '🟢 優良', 'liq_msg': '流動性極佳 (Spread: 7.7%) | 建議：可嘗試掛 Mid-price 或微偏 Ask 成交',
        }

        # 9. 倉位 — AROC 達標 (≥15%)
        mock_sizing.return_value = (22.0, 0.04, 13730.0)

        # ACT
        result = strategy.analyze_symbol("OVERSOLD_STOCK")

        # ASSERT — 管線結果
        self.assertIsNotNone(result, "STO_PUT 管線不應回傳 None")
        self.assertEqual(result['strategy'], "STO_PUT")
        self.assertEqual(result['symbol'], "OVERSOLD_STOCK")
        self.assertAlmostEqual(result['price'], 150.0)
        self.assertGreater(result['alloc_pct'], 0)
        self.assertGreaterEqual(result['aroc'], 15.0)

        # ASSERT — Discord Embed
        embed = create_scan_embed(result, user_capital=50000.0)
        _assert_embed_valid(self, embed, "STO_PUT", "OVERSOLD_STOCK")
        # STO_PUT 應有機率圓錐 field
        cone_fields = [f for f in embed.fields if "機率圓錐" in f.name]
        self.assertTrue(len(cone_fields) > 0, "STO_PUT Embed 應包含機率圓錐欄位")
        # STO_PUT 不應有策略升級建議（只有買方才有）
        upgrade_fields = [f for f in embed.fields if "策略升級" in f.name]
        self.assertEqual(len(upgrade_fields), 0, "STO_PUT 不應有策略升級建議")

    # ==============================
    # 情境 2: STO_CALL — 超買收入
    # ==============================
    @patch('market_analysis.strategy._calculate_technical_indicators')
    @patch('market_analysis.strategy._determine_strategy_signal')
    @patch('market_analysis.strategy._calculate_mmm')
    @patch('market_analysis.strategy._calculate_term_structure')
    @patch('market_analysis.strategy._find_target_expiry')
    @patch('market_analysis.strategy._get_best_contract_data')
    @patch('market_analysis.strategy._calculate_vertical_skew')
    @patch('market_analysis.strategy._validate_risk_and_liquidity')
    @patch('market_analysis.strategy._calculate_sizing')
    @patch('market_analysis.strategy.yf.Ticker')
    def test_scenario_sto_call(self, mock_ticker_cls, mock_sizing, mock_validate,
                                mock_skew, mock_contract, mock_expiry, mock_ts,
                                mock_mmm, mock_signal, mock_indicators):
        """
        STO_CALL 情境：RSI=70 (超買), HV Rank=40 (高波動)
        預期：策略為 STO_CALL，賣 Call δ≈+0.20，DTE 30–45
        最終驗證 create_scan_embed 產出合法的 Discord Embed
        """
        mock_ticker = MagicMock()
        mock_ticker_cls.return_value = mock_ticker
        mock_ticker.history.return_value = MagicMock()
        mock_ticker.options = ["2026-03-20", "2026-04-17"]

        # 1. 技術指標 — 超買
        mock_indicators.return_value = {
            'price': 200.0, 'rsi': 70.0, 'sma20': 190.0,
            'hv_current': 0.28, 'hv_rank': 40.0, 'macd_hist': 2.0,
        }

        # 2. 策略訊號 — STO_CALL
        mock_signal.return_value = ("STO_CALL", "call", 0.20, 30, 45)

        # 3. MMM — 無財報
        mock_mmm.return_value = (0.0, 0.0, 0.0, -1)

        # 4. 期限結構 — 正常
        mock_ts.return_value = (0.95, "🌊 正常 (Contango)")

        # 5. 到期日
        mock_expiry.return_value = ("2026-04-17", 40)

        # 6. 最佳合約
        best = _make_mock_contract(strike=215.0, bid=3.00, ask=3.20, bs_delta=0.19, iv=0.32)
        mock_contract.return_value = (best, MagicMock())

        # 7. 偏態 — 中性
        mock_skew.return_value = (1.08, "⚖️ 中性 (Neutral)")

        # 8. 風險/流動性 — 全通過
        mock_validate.return_value = {
            'bid': 3.00, 'ask': 3.20, 'spread': 0.20, 'spread_ratio': 6.5,
            'vrp': 0.04, 'expected_move': 15.0, 'em_lower': 185.0, 'em_upper': 215.0,
            'mid_price': 3.10, 'suggested_hedge_strike': None,
            'liq_status': '🟢 優良', 'liq_msg': '流動性極佳 (Spread: 6.5%) | 建議：可嘗試掛 Mid-price 或微偏 Ask 成交',
        }

        # 9. 倉位 — AROC ≥ 15%
        mock_sizing.return_value = (18.5, 0.03, 21200.0)

        # ACT
        result = strategy.analyze_symbol("OVERBOUGHT_STOCK")

        # ASSERT — 管線結果
        self.assertIsNotNone(result, "STO_CALL 管線不應回傳 None")
        self.assertEqual(result['strategy'], "STO_CALL")
        self.assertEqual(result['symbol'], "OVERBOUGHT_STOCK")
        self.assertAlmostEqual(result['price'], 200.0)
        self.assertGreater(result['alloc_pct'], 0)
        self.assertGreaterEqual(result['aroc'], 15.0)

        # ASSERT — Discord Embed
        embed = create_scan_embed(result, user_capital=50000.0)
        _assert_embed_valid(self, embed, "STO_CALL", "OVERBOUGHT_STOCK")
        # STO_CALL 有機率圓錐
        cone_fields = [f for f in embed.fields if "機率圓錐" in f.name]
        self.assertTrue(len(cone_fields) > 0, "STO_CALL Embed 應包含機率圓錐欄位")

    # ==============================
    # 情境 3: BTO_CALL — 動能突破
    # ==============================
    @patch('market_analysis.strategy._calculate_technical_indicators')
    @patch('market_analysis.strategy._determine_strategy_signal')
    @patch('market_analysis.strategy._calculate_mmm')
    @patch('market_analysis.strategy._calculate_term_structure')
    @patch('market_analysis.strategy._find_target_expiry')
    @patch('market_analysis.strategy._get_best_contract_data')
    @patch('market_analysis.strategy._calculate_vertical_skew')
    @patch('market_analysis.strategy._validate_risk_and_liquidity')
    @patch('market_analysis.strategy._calculate_sizing')
    @patch('market_analysis.strategy.yf.Ticker')
    def test_scenario_bto_call(self, mock_ticker_cls, mock_sizing, mock_validate,
                                mock_skew, mock_contract, mock_expiry, mock_ts,
                                mock_mmm, mock_signal, mock_indicators):
        """
        BTO_CALL 情境：Price > SMA20, RSI=55, MACD > 0, HV Rank=30 (低波動)
        預期：策略為 BTO_CALL，買 Call δ≈+0.50 (ATM)，DTE 30–60
        最終驗證 create_scan_embed 產出含策略升級建議的 Discord Embed
        """
        mock_ticker = MagicMock()
        mock_ticker_cls.return_value = mock_ticker
        mock_ticker.history.return_value = MagicMock()
        mock_ticker.options = ["2026-03-20", "2026-04-17", "2026-05-15"]

        # 1. 技術指標 — 多頭趨勢 + 低波動
        mock_indicators.return_value = {
            'price': 180.0, 'rsi': 55.0, 'sma20': 170.0,
            'hv_current': 0.20, 'hv_rank': 30.0, 'macd_hist': 1.2,
        }

        # 2. 策略訊號 — BTO_CALL
        mock_signal.return_value = ("BTO_CALL", "call", 0.50, 30, 60)

        # 3. MMM — 無財報
        mock_mmm.return_value = (0.0, 0.0, 0.0, -1)

        # 4. 期限結構 — 平滑
        mock_ts.return_value = (1.0, "平滑 (Flat)")

        # 5. 到期日
        mock_expiry.return_value = ("2026-04-17", 45)

        # 6. 最佳合約 — ATM
        best = _make_mock_contract(strike=180.0, bid=6.50, ask=6.80, bs_delta=0.52, iv=0.25)
        mock_contract.return_value = (best, MagicMock())

        # 7. 偏態 — 中性
        mock_skew.return_value = (1.02, "⚖️ 中性 (Neutral)")

        # 8. 風險/流動性 — 全通過 (買方 VRP ≤ 3%)
        mock_validate.return_value = {
            'bid': 6.50, 'ask': 6.80, 'spread': 0.30, 'spread_ratio': 4.5,
            'vrp': 0.01, 'expected_move': 10.0, 'em_lower': 170.0, 'em_upper': 190.0,
            'mid_price': 6.65, 'suggested_hedge_strike': 190.0,
            'liq_status': '🟡 尚可', 'liq_msg': '流動性普通 (Spread: 4.5%) | 建議：嚴格掛 Mid-price 等待成交',
        }

        # 9. 倉位 — 買方 AROC ≥ 30%
        mock_sizing.return_value = (45.0, 0.02, 680.0)

        # ACT
        result = strategy.analyze_symbol("MOMENTUM_STOCK")

        # ASSERT — 管線結果
        self.assertIsNotNone(result, "BTO_CALL 管線不應回傳 None")
        self.assertEqual(result['strategy'], "BTO_CALL")
        self.assertEqual(result['symbol'], "MOMENTUM_STOCK")
        self.assertAlmostEqual(result['price'], 180.0)
        self.assertGreater(result['alloc_pct'], 0)
        self.assertGreaterEqual(result['aroc'], 30.0)
        self.assertIsNotNone(result['suggested_hedge_strike'])
        self.assertAlmostEqual(result['suggested_hedge_strike'], 190.0)

        # ASSERT — Discord Embed
        embed = create_scan_embed(result, user_capital=50000.0)
        _assert_embed_valid(self, embed, "BTO_CALL", "MOMENTUM_STOCK")
        # BTO_CALL 應有策略升級建議 (Bull Call Spread)
        upgrade_fields = [f for f in embed.fields if "策略升級" in f.name]
        self.assertTrue(len(upgrade_fields) > 0, "BTO_CALL Embed 應包含策略升級建議")
        self.assertIn("Bull Call Spread", upgrade_fields[0].value)
        self.assertIn("190", upgrade_fields[0].value)
        # BTO_CALL 有機率圓錐
        cone_fields = [f for f in embed.fields if "機率圓錐" in f.name]
        self.assertTrue(len(cone_fields) > 0, "BTO_CALL Embed 應包含機率圓錐欄位")

    # ==============================
    # 情境 4: BTO_PUT — 跌破避險
    # ==============================
    @patch('market_analysis.strategy._calculate_technical_indicators')
    @patch('market_analysis.strategy._determine_strategy_signal')
    @patch('market_analysis.strategy._calculate_mmm')
    @patch('market_analysis.strategy._calculate_term_structure')
    @patch('market_analysis.strategy._find_target_expiry')
    @patch('market_analysis.strategy._get_best_contract_data')
    @patch('market_analysis.strategy._calculate_vertical_skew')
    @patch('market_analysis.strategy._validate_risk_and_liquidity')
    @patch('market_analysis.strategy._calculate_sizing')
    @patch('market_analysis.strategy.yf.Ticker')
    def test_scenario_bto_put(self, mock_ticker_cls, mock_sizing, mock_validate,
                               mock_skew, mock_contract, mock_expiry, mock_ts,
                               mock_mmm, mock_signal, mock_indicators):
        """
        BTO_PUT 情境：Price < SMA20, RSI=42, MACD < 0, HV Rank=30 (低波動、剛起跌)
        預期：策略為 BTO_PUT，買 Put δ≈−0.50 (ATM)，DTE 30–60
        最終驗證 create_scan_embed 產出含策略升級建議的 Discord Embed
        """
        mock_ticker = MagicMock()
        mock_ticker_cls.return_value = mock_ticker
        mock_ticker.history.return_value = MagicMock()
        mock_ticker.options = ["2026-03-20", "2026-04-17", "2026-05-15"]

        # 1. 技術指標 — 空頭趨勢 + 低波動
        mock_indicators.return_value = {
            'price': 120.0, 'rsi': 42.0, 'sma20': 130.0,
            'hv_current': 0.22, 'hv_rank': 30.0, 'macd_hist': -0.8,
        }

        # 2. 策略訊號 — BTO_PUT
        mock_signal.return_value = ("BTO_PUT", "put", -0.50, 30, 60)

        # 3. MMM — 無財報
        mock_mmm.return_value = (0.0, 0.0, 0.0, -1)

        # 4. 期限結構 — 平滑
        mock_ts.return_value = (1.0, "平滑 (Flat)")

        # 5. 到期日
        mock_expiry.return_value = ("2026-04-17", 45)

        # 6. 最佳合約 — ATM Put
        best = _make_mock_contract(strike=120.0, bid=5.80, ask=6.10, bs_delta=-0.48, iv=0.26)
        mock_contract.return_value = (best, MagicMock())

        # 7. 偏態 — 輕微左偏但不觸發否決
        mock_skew.return_value = (1.15, "⚖️ 中性 (Neutral)")

        # 8. 風險/流動性 — 全通過 (買方 VRP ≤ 3%)
        mock_validate.return_value = {
            'bid': 5.80, 'ask': 6.10, 'spread': 0.30, 'spread_ratio': 5.0,
            'vrp': 0.02, 'expected_move': 9.0, 'em_lower': 111.0, 'em_upper': 129.0,
            'mid_price': 5.95, 'suggested_hedge_strike': 111.0,
            'liq_status': '🟡 尚可', 'liq_msg': '流動性普通 (Spread: 5.0%) | 建議：嚴格掛 Mid-price 等待成交',
        }

        # 9. 倉位 — 買方 AROC ≥ 30%
        mock_sizing.return_value = (38.0, 0.015, 610.0)

        # ACT
        result = strategy.analyze_symbol("BREAKDOWN_STOCK")

        # ASSERT — 管線結果
        self.assertIsNotNone(result, "BTO_PUT 管線不應回傳 None")
        self.assertEqual(result['strategy'], "BTO_PUT")
        self.assertEqual(result['symbol'], "BREAKDOWN_STOCK")
        self.assertAlmostEqual(result['price'], 120.0)
        self.assertGreater(result['alloc_pct'], 0)
        self.assertGreaterEqual(result['aroc'], 30.0)
        self.assertIsNotNone(result['suggested_hedge_strike'])
        self.assertAlmostEqual(result['suggested_hedge_strike'], 111.0)

        # ASSERT — Discord Embed
        embed = create_scan_embed(result, user_capital=50000.0)
        _assert_embed_valid(self, embed, "BTO_PUT", "BREAKDOWN_STOCK")
        # BTO_PUT 應有策略升級建議 (Bear Put Spread)
        upgrade_fields = [f for f in embed.fields if "策略升級" in f.name]
        self.assertTrue(len(upgrade_fields) > 0, "BTO_PUT Embed 應包含策略升級建議")
        self.assertIn("Bear Put Spread", upgrade_fields[0].value)
        self.assertIn("111", upgrade_fields[0].value)
        # BTO_PUT 有機率圓錐
        cone_fields = [f for f in embed.fields if "機率圓錐" in f.name]
        self.assertTrue(len(cone_fields) > 0, "BTO_PUT Embed 應包含機率圓錐欄位")


# ====================================================================
# 額外：直接測試 _determine_strategy_signal 四種分支
# ====================================================================
class TestDetermineStrategySignalAllBranches(unittest.TestCase):
    """直接以 indicator dict 驅動 _determine_strategy_signal，驗證四種分支"""

    def test_sto_put_branch(self):
        """RSI < 35, HV Rank ≥ 30 → STO_PUT"""
        ind = {'price': 150.0, 'rsi': 30, 'hv_rank': 40, 'sma20': 160.0, 'macd_hist': -1.0}
        strat, opt, delta, min_d, max_d = strategy._determine_strategy_signal(ind)
        self.assertEqual(strat, "STO_PUT")
        self.assertEqual(opt, "put")
        self.assertAlmostEqual(delta, -0.16)
        self.assertEqual(min_d, 30)
        self.assertEqual(max_d, 45)

    def test_sto_call_branch(self):
        """RSI > 65, HV Rank ≥ 30 → STO_CALL"""
        ind = {'price': 200.0, 'rsi': 70, 'hv_rank': 40, 'sma20': 190.0, 'macd_hist': 2.0}
        strat, opt, delta, min_d, max_d = strategy._determine_strategy_signal(ind)
        self.assertEqual(strat, "STO_CALL")
        self.assertEqual(opt, "call")
        self.assertAlmostEqual(delta, 0.16)
        self.assertEqual(min_d, 30)
        self.assertEqual(max_d, 45)

    def test_bto_call_branch(self):
        """Price > SMA20, 50 ≤ RSI ≤ 65, MACD > 0, HV Rank < 50 → BTO_CALL"""
        ind = {'price': 180.0, 'rsi': 55, 'hv_rank': 30, 'sma20': 170.0, 'macd_hist': 1.5}
        strat, opt, delta, min_d, max_d = strategy._determine_strategy_signal(ind)
        self.assertEqual(strat, "BTO_CALL")
        self.assertEqual(opt, "call")
        self.assertAlmostEqual(delta, 0.50)

    def test_bto_call_high_hv_switches_to_sto_put(self):
        """Price > SMA20, 50 ≤ RSI ≤ 65, MACD > 0, HV Rank ≥ 50 → 動態切換為 STO_PUT"""
        ind = {'price': 180.0, 'rsi': 55, 'hv_rank': 55, 'sma20': 170.0, 'macd_hist': 1.5}
        strat, opt, delta, min_d, max_d = strategy._determine_strategy_signal(ind)
        self.assertEqual(strat, "STO_PUT")
        self.assertEqual(opt, "put")

    def test_bto_put_branch(self):
        """Price < SMA20, 35 ≤ RSI ≤ 50, MACD < 0, HV Rank < 50 → BTO_PUT"""
        ind = {'price': 120.0, 'rsi': 42, 'hv_rank': 30, 'sma20': 130.0, 'macd_hist': -0.8}
        strat, opt, delta, min_d, max_d = strategy._determine_strategy_signal(ind)
        self.assertEqual(strat, "BTO_PUT")
        self.assertEqual(opt, "put")
        self.assertAlmostEqual(delta, -0.50)

    def test_bto_put_high_hv_switches_to_sto_call(self):
        """Price < SMA20, 35 ≤ RSI ≤ 50, MACD < 0, HV Rank ≥ 50 → 動態切換為 STO_CALL"""
        ind = {'price': 120.0, 'rsi': 42, 'hv_rank': 55, 'sma20': 130.0, 'macd_hist': -0.8}
        strat, opt, delta, min_d, max_d = strategy._determine_strategy_signal(ind)
        self.assertEqual(strat, "STO_CALL")
        self.assertEqual(opt, "call")

    def test_no_signal(self):
        """不符合任何條件 → None"""
        ind = {'price': 150.0, 'rsi': 50, 'hv_rank': 20, 'sma20': 150.0, 'macd_hist': 0}
        strat, opt, delta, min_d, max_d = strategy._determine_strategy_signal(ind)
        self.assertIsNone(strat)


if __name__ == '__main__':
    unittest.main()
