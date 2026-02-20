import unittest
from unittest.mock import MagicMock, patch, PropertyMock
import sys
from types import ModuleType
from datetime import datetime, date

# --- MOCK DEPENDENCIES BEFORE IMPORTING PORTFOLIO ---

# Mock pandas
mock_pd = MagicMock()
sys.modules["pandas"] = mock_pd

# Mock yfinance (set __spec__ to avoid breaking importlib.util.find_spec)
mock_yf = MagicMock()
mock_yf.__spec__ = None
sys.modules["yfinance"] = mock_yf

# Mock pandas_ta (prevent it from importing yfinance via find_spec)
mock_pandas_ta = MagicMock()
sys.modules["pandas_ta"] = mock_pandas_ta

# Mock market_analysis.strategy to prevent __init__.py import chain
mock_strategy = MagicMock()
mock_strategy.analyze_symbol = MagicMock()
sys.modules["market_analysis.strategy"] = mock_strategy

# Mock market_analysis.greeks and market_analysis.data
mock_greeks_mod = MagicMock()
sys.modules["market_analysis.greeks"] = mock_greeks_mod
mock_data_mod = MagicMock()
sys.modules["market_analysis.data"] = mock_data_mod

# Mock py_vollib and all submodules
mock_vollib = MagicMock()
sys.modules["py_vollib"] = mock_vollib
sys.modules["py_vollib.black_scholes_merton"] = mock_vollib
sys.modules["py_vollib.black_scholes_merton.greeks"] = mock_vollib
sys.modules["py_vollib.black_scholes_merton.greeks.analytical"] = mock_vollib

# Mock config
mock_config = ModuleType("config")
mock_config.RISK_FREE_RATE = 0.042
sys.modules["config"] = mock_config

# Now import portfolio
import os
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))
from market_analysis.portfolio import (
    check_portfolio_status_logic,
    _evaluate_defense_status,
    _calculate_macro_risk,
)


# ====================================================================
# Helper: 建構 mock 的 yf.Ticker 物件與 option chain
# ====================================================================
def _build_mock_ticker(stock_price=150.0, beta=1.0, dividend_yield=0.01,
                       option_chain_data=None):
    """回傳一個設定好的 mock Ticker 物件"""
    ticker = MagicMock()
    hist_df = MagicMock()
    hist_df.empty = False
    hist_df.__getitem__ = MagicMock(
        return_value=MagicMock(__getitem__=MagicMock(return_value=stock_price))
    )
    # hist['Close'].iloc[-1] -> stock_price
    close_series = MagicMock()
    close_series.iloc.__getitem__ = MagicMock(return_value=stock_price)
    hist_df.__getitem__ = MagicMock(return_value=close_series)
    ticker.history.return_value = hist_df

    ticker.info = {'beta': beta, 'dividendYield': dividend_yield}

    if option_chain_data is not None:
        chain = MagicMock()
        calls_df = MagicMock()
        puts_df = MagicMock()

        def _make_contract(last_price, iv):
            contract_row = MagicMock()
            contract_row.empty = False
            contract_row.__getitem__ = MagicMock(side_effect=lambda col: {
                'lastPrice': MagicMock(iloc=MagicMock(__getitem__=MagicMock(return_value=last_price))),
                'impliedVolatility': MagicMock(iloc=MagicMock(__getitem__=MagicMock(return_value=iv))),
            }[col])
            return contract_row

        # 根據 strike 回傳對應的 contract mock
        call_data = option_chain_data.get('call', {})
        put_data = option_chain_data.get('put', {})

        def _calls_filter(strike_col):
            _m = MagicMock()
            def _eq(strike_val):
                if strike_val in call_data:
                    return _make_contract(call_data[strike_val]['lastPrice'],
                                          call_data[strike_val]['iv'])
                empty = MagicMock()
                empty.empty = True
                return empty
            _m.__eq__ = MagicMock(side_effect=_eq)
            return _m

        def _puts_filter(strike_col):
            _m = MagicMock()
            def _eq(strike_val):
                if strike_val in put_data:
                    return _make_contract(put_data[strike_val]['lastPrice'],
                                          put_data[strike_val]['iv'])
                empty = MagicMock()
                empty.empty = True
                return empty
            _m.__eq__ = MagicMock(side_effect=_eq)
            return _m

        calls_df.__getitem__ = MagicMock(side_effect=_calls_filter)
        puts_df.__getitem__ = MagicMock(side_effect=_puts_filter)
        chain.calls = calls_df
        chain.puts = puts_df
        ticker.option_chain.return_value = chain

    return ticker


# ====================================================================
# Tests for _evaluate_defense_status (純函式, 無需 network mock)
# ====================================================================
class TestEvaluateDefenseStatus(unittest.TestCase):
    """直接測試動態防禦決策樹的每個分支"""

    # ------------- 賣方 (quantity < 0) -------------
    def test_short_take_profit(self):
        """賣方獲利 ≥50% → 建議停利"""
        status = _evaluate_defense_status(-1, 'put', 0.50, -0.10, 45)
        self.assertIn("停利", status)
        self.assertIn("50%", status)

    def test_short_black_swan(self):
        """賣方虧損 ≥150% → 黑天鵝強制停損"""
        status = _evaluate_defense_status(-1, 'put', -1.50, -0.10, 45)
        self.assertIn("黑天鵝", status)
        self.assertIn("停損", status)

    def test_short_put_delta_expansion(self):
        """賣 Put Delta ≤ -0.40 → Roll Down and Out"""
        status = _evaluate_defense_status(-1, 'put', -0.30, -0.40, 45)
        self.assertIn("Roll Down and Out", status)

    def test_short_call_delta_expansion(self):
        """賣 Call Delta ≥ 0.40 → Roll Up & Out"""
        status = _evaluate_defense_status(-1, 'call', -0.30, 0.40, 45)
        self.assertIn("Roll Up & Out", status)

    def test_short_gamma_trap(self):
        """賣方 DTE ≤ 21 且無其他觸發 → Gamma 陷阱"""
        status = _evaluate_defense_status(-1, 'put', 0.10, -0.10, 21)
        self.assertIn("Gamma", status)
        self.assertIn("21", status)

    def test_short_hold(self):
        """賣方無觸發條件 → 繼續持有"""
        status = _evaluate_defense_status(-1, 'put', 0.10, -0.10, 45)
        self.assertIn("繼續持有", status)

    # ------------- 買方 (quantity > 0) -------------
    def test_long_take_profit(self):
        """買方獲利 ≥100% → 建議停利"""
        status = _evaluate_defense_status(1, 'call', 1.0, 0.60, 45)
        self.assertIn("停利", status)
        self.assertIn("100%", status)

    def test_long_stop_loss(self):
        """買方虧損 ≥50% → 停損警戒"""
        status = _evaluate_defense_status(1, 'call', -0.50, 0.30, 45)
        self.assertIn("停損", status)

    def test_long_momentum_decay(self):
        """買方 DTE ≤ 21 且無觸發停利/停損 → 動能衰竭"""
        status = _evaluate_defense_status(1, 'call', 0.10, 0.30, 21)
        self.assertIn("動能衰竭", status)

    def test_long_hold(self):
        """買方無觸發條件 → 繼續持有"""
        status = _evaluate_defense_status(1, 'call', 0.10, 0.30, 45)
        self.assertIn("繼續持有", status)

    # ------------- 邊界值確認 (boundary conditions) -----
    def test_short_take_profit_boundary_just_below(self):
        """pnl_pct = 0.49 → 不觸發停利"""
        status = _evaluate_defense_status(-1, 'put', 0.49, -0.10, 45)
        self.assertIn("繼續持有", status)

    def test_short_black_swan_boundary_just_above(self):
        """pnl_pct = -1.49 → 不觸發黑天鵝"""
        status = _evaluate_defense_status(-1, 'put', -1.49, -0.10, 45)
        self.assertIn("繼續持有", status)

    def test_short_gamma_trap_dte_22(self):
        """DTE = 22 → 不觸發 Gamma 陷阱"""
        status = _evaluate_defense_status(-1, 'put', 0.10, -0.10, 22)
        self.assertIn("繼續持有", status)

    def test_short_priority_profit_over_delta(self):
        """停利的優先順序高於 Delta 擴張"""
        status = _evaluate_defense_status(-1, 'put', 0.50, -0.50, 21)
        self.assertIn("停利", status)

    def test_short_priority_black_swan_over_delta(self):
        """黑天鵝的優先順序高於 Delta 擴張"""
        status = _evaluate_defense_status(-1, 'put', -1.50, -0.50, 21)
        self.assertIn("黑天鵝", status)


# ====================================================================
# Tests for _calculate_macro_risk (純函式)
# ====================================================================
class TestCalculateMacroRisk(unittest.TestCase):
    """測試宏觀風險報告的各情境"""

    def test_delta_neutral(self):
        """淨 Delta ∈ (-50, 50) → 風險中性"""
        lines = _calculate_macro_risk(0, -5.0, 5000, 0, 50000)
        joined = "\n".join(lines)
        self.assertIn("風險中性", joined)

    def test_delta_bullish_alert(self):
        """淨 Delta > 50 → 多頭曝險過高"""
        lines = _calculate_macro_risk(51, -5.0, 5000, 0, 50000)
        joined = "\n".join(lines)
        self.assertIn("多頭曝險過高", joined)

    def test_delta_bearish_alert(self):
        """淨 Delta < -50 → 空頭曝險過高"""
        lines = _calculate_macro_risk(-51, -5.0, 5000, 0, 50000)
        joined = "\n".join(lines)
        self.assertIn("空頭曝險過高", joined)

    def test_gamma_fragile(self):
        """淨 Gamma < -20 → 脆性警告"""
        lines = _calculate_macro_risk(0, -5.0, 5000, -21, 50000)
        joined = "\n".join(lines)
        self.assertIn("脆性警告", joined)

    def test_gamma_antifragile(self):
        """淨 Gamma > 20 → 反脆弱"""
        lines = _calculate_macro_risk(0, -5.0, 5000, 21, 50000)
        joined = "\n".join(lines)
        self.assertIn("反脆弱", joined)

    def test_gamma_neutral(self):
        """淨 Gamma ∈ [-20, 20] → Gamma 中性"""
        lines = _calculate_macro_risk(0, -5.0, 5000, 0, 50000)
        joined = "\n".join(lines)
        self.assertIn("Gamma 中性", joined)

    def test_theta_yield_low(self):
        """Theta 收益率 < 0.05% → 利用率過低"""
        # theta/capital * 100 < 0.05  →  theta < 25
        lines = _calculate_macro_risk(0, -10.0, 5000, 0, 50000)
        joined = "\n".join(lines)
        self.assertIn("資金利用率過低", joined)

    def test_theta_yield_high(self):
        """Theta 收益率 > 0.30% → 曝險過度"""
        # theta/capital * 100 > 0.30  →  theta > 150
        lines = _calculate_macro_risk(0, 200, 5000, 0, 50000)
        joined = "\n".join(lines)
        self.assertIn("曝險過度", joined)

    def test_theta_yield_healthy(self):
        """Theta 收益率 ∈ [0.05%, 0.30%] → 健康"""
        # theta = 50 → 50/50000*100 = 0.1%
        lines = _calculate_macro_risk(0, 50, 5000, 0, 50000)
        joined = "\n".join(lines)
        self.assertIn("現金流健康", joined)

    def test_portfolio_heat_explosion(self):
        """Heat > 50% → 爆倉警戒"""
        lines = _calculate_macro_risk(0, 50, 30000, 0, 50000)
        joined = "\n".join(lines)
        self.assertIn("爆倉警戒", joined)

    def test_portfolio_heat_warning(self):
        """Heat ∈ (30%, 50%] → 資金警戒"""
        # 20000/50000*100 = 40%
        lines = _calculate_macro_risk(0, 50, 20000, 0, 50000)
        joined = "\n".join(lines)
        self.assertIn("資金警戒", joined)

    def test_portfolio_heat_healthy(self):
        """Heat ≤ 30% → 健康"""
        # 10000/50000*100 = 20%
        lines = _calculate_macro_risk(0, 50, 10000, 0, 50000)
        joined = "\n".join(lines)
        self.assertIn("資金水位健康", joined)

    def test_zero_capital(self):
        """user_capital = 0 → 不應 ZeroDivisionError"""
        lines = _calculate_macro_risk(0, 0, 0, 0, 0)
        self.assertIsInstance(lines, list)


# ====================================================================
# Tests for check_portfolio_status_logic (Orchestrator — 需 mock 外部)
# ====================================================================
class TestCheckPortfolioStatusLogic(unittest.TestCase):
    """測試主 orchestrator 函式"""

    def test_empty_portfolio(self):
        """空列表 → 回傳空列表"""
        result = check_portfolio_status_logic([])
        self.assertEqual(result, [])

    def test_none_portfolio(self):
        """None → 回傳空列表 (falsy check)"""
        result = check_portfolio_status_logic(None)
        self.assertEqual(result, [])

    @patch('market_analysis.portfolio._analyze_correlation', return_value=[])
    @patch('market_analysis.portfolio._calculate_macro_risk', return_value=["macro_line"])
    @patch('market_analysis.portfolio._evaluate_defense_status', return_value="⏳ 繼續持有")
    @patch('market_analysis.portfolio.gamma', return_value=0.05)
    @patch('market_analysis.portfolio.theta', return_value=-0.02)
    @patch('market_analysis.portfolio.delta', return_value=-0.15)
    @patch('market_analysis.portfolio.datetime')
    @patch('market_analysis.portfolio.yf')
    def test_single_short_put_generates_report(self, mock_yf_mod, mock_dt,
                                                mock_delta, mock_theta, mock_gamma,
                                                mock_defense, mock_macro, mock_corr):
        """單一賣 Put 部位 → 應產生包含該標的的報告行"""
        # 固定 datetime.now()
        mock_dt.now.return_value = datetime(2025, 1, 15)
        mock_dt.strptime = datetime.strptime

        # SPY price
        spy_ticker = MagicMock()
        spy_hist = MagicMock()
        spy_close = MagicMock()
        spy_close.iloc.__getitem__ = MagicMock(return_value=500.0)
        spy_hist.__getitem__ = MagicMock(return_value=spy_close)
        spy_hist.empty = False

        # Symbol ticker
        sym_ticker = MagicMock()
        sym_hist = MagicMock()
        sym_hist.empty = False
        sym_close = MagicMock()
        sym_close.iloc.__getitem__ = MagicMock(return_value=150.0)
        sym_hist.__getitem__ = MagicMock(return_value=sym_close)
        sym_ticker.history.return_value = sym_hist
        sym_ticker.info = {'beta': 1.0, 'dividendYield': 0.01}

        # option chain
        contract = MagicMock()
        contract.empty = False
        last_price_series = MagicMock()
        last_price_series.iloc.__getitem__ = MagicMock(return_value=2.50)
        iv_series = MagicMock()
        iv_series.iloc.__getitem__ = MagicMock(return_value=0.25)
        contract.__getitem__ = MagicMock(side_effect=lambda k: {
            'lastPrice': last_price_series,
            'impliedVolatility': iv_series,
        }[k])

        chain = MagicMock()
        puts_df = MagicMock()
        puts_df.__getitem__ = MagicMock(return_value=contract)
        chain.puts = puts_df
        chain.calls = MagicMock()
        sym_ticker.option_chain.return_value = chain

        def ticker_factory(sym):
            if sym == "SPY":
                spy_t = MagicMock()
                spy_t.history.return_value = spy_hist
                return spy_t
            return sym_ticker

        mock_yf_mod.Ticker.side_effect = ticker_factory

        # Portfolio: (symbol, opt_type, strike, expiry, entry_price, quantity, stock_cost)
        rows = [("AAPL", "put", 140.0, "2025-03-21", 3.50, -1, 0.0)]

        result = check_portfolio_status_logic(rows, user_capital=50000)

        # 驗證報告行包含標的名稱
        report_text = "\n".join(result)
        self.assertIn("AAPL", report_text)
        # 驗證 _evaluate_defense_status 被呼叫
        mock_defense.assert_called_once()
        # 驗證 _calculate_macro_risk 被呼叫
        mock_macro.assert_called_once()

    @patch('market_analysis.portfolio._analyze_correlation', return_value=[])
    @patch('market_analysis.portfolio._calculate_macro_risk', return_value=["macro_line"])
    @patch('market_analysis.portfolio._evaluate_defense_status', return_value="⏳ 繼續持有")
    @patch('market_analysis.portfolio.gamma', return_value=0.05)
    @patch('market_analysis.portfolio.theta', return_value=-0.02)
    @patch('market_analysis.portfolio.delta', return_value=-0.15)
    @patch('market_analysis.portfolio.datetime')
    @patch('market_analysis.portfolio.yf')
    def test_short_put_margin_passed_to_macro(self, mock_yf_mod, mock_dt,
                                               mock_delta, mock_theta, mock_gamma,
                                               mock_defense, mock_macro, mock_corr):
        """賣 Put 的 margin = strike * 100 * abs(quantity) 應正確傳遞到 macro risk"""
        mock_dt.now.return_value = datetime(2025, 1, 15)
        mock_dt.strptime = datetime.strptime

        spy_hist = MagicMock()
        spy_close = MagicMock()
        spy_close.iloc.__getitem__ = MagicMock(return_value=500.0)
        spy_hist.__getitem__ = MagicMock(return_value=spy_close)
        spy_hist.empty = False

        sym_ticker = MagicMock()
        sym_hist = MagicMock()
        sym_hist.empty = False
        sym_close = MagicMock()
        sym_close.iloc.__getitem__ = MagicMock(return_value=150.0)
        sym_hist.__getitem__ = MagicMock(return_value=sym_close)
        sym_ticker.history.return_value = sym_hist
        sym_ticker.info = {'beta': 1.0, 'dividendYield': 0.01}

        contract = MagicMock()
        contract.empty = False
        last_price_series = MagicMock()
        last_price_series.iloc.__getitem__ = MagicMock(return_value=2.50)
        iv_series = MagicMock()
        iv_series.iloc.__getitem__ = MagicMock(return_value=0.25)
        contract.__getitem__ = MagicMock(side_effect=lambda k: {
            'lastPrice': last_price_series,
            'impliedVolatility': iv_series,
        }[k])

        chain = MagicMock()
        puts_df = MagicMock()
        puts_df.__getitem__ = MagicMock(return_value=contract)
        chain.puts = puts_df
        chain.calls = MagicMock()
        sym_ticker.option_chain.return_value = chain

        def ticker_factory(sym):
            if sym == "SPY":
                spy_t = MagicMock()
                spy_t.history.return_value = spy_hist
                return spy_t
            return sym_ticker

        mock_yf_mod.Ticker.side_effect = ticker_factory

        strike = 140.0
        qty = -2
        rows = [("AAPL", "put", strike, "2025-03-21", 3.50, qty, 0.0)]
        check_portfolio_status_logic(rows, user_capital=50000)

        # margin for short put = strike * 100 * abs(qty) = 140 * 100 * 2 = 28000
        expected_margin = strike * 100 * abs(qty)
        call_args = mock_macro.call_args
        actual_margin = call_args[0][2]  # 第三個位置引數
        self.assertAlmostEqual(actual_margin, expected_margin, places=2)

    @patch('market_analysis.portfolio._analyze_correlation', return_value=["corr_line"])
    @patch('market_analysis.portfolio._calculate_macro_risk', return_value=["macro_line"])
    @patch('market_analysis.portfolio._evaluate_defense_status', return_value="⏳ 繼續持有")
    @patch('market_analysis.portfolio.gamma', return_value=0.05)
    @patch('market_analysis.portfolio.theta', return_value=-0.02)
    @patch('market_analysis.portfolio.delta', return_value=-0.15)
    @patch('market_analysis.portfolio.datetime')
    @patch('market_analysis.portfolio.yf')
    def test_multiple_symbols_triggers_correlation(self, mock_yf_mod, mock_dt,
                                                    mock_delta, mock_theta, mock_gamma,
                                                    mock_defense, mock_macro, mock_corr):
        """多個不同標的 → _analyze_correlation 被呼叫且傳入正確 symbols"""
        mock_dt.now.return_value = datetime(2025, 1, 15)
        mock_dt.strptime = datetime.strptime

        spy_hist = MagicMock()
        spy_close = MagicMock()
        spy_close.iloc.__getitem__ = MagicMock(return_value=500.0)
        spy_hist.__getitem__ = MagicMock(return_value=spy_close)
        spy_hist.empty = False

        sym_ticker = MagicMock()
        sym_hist = MagicMock()
        sym_hist.empty = False
        sym_close = MagicMock()
        sym_close.iloc.__getitem__ = MagicMock(return_value=150.0)
        sym_hist.__getitem__ = MagicMock(return_value=sym_close)
        sym_ticker.history.return_value = sym_hist
        sym_ticker.info = {'beta': 1.0, 'dividendYield': 0.01}

        contract = MagicMock()
        contract.empty = False
        last_price_series = MagicMock()
        last_price_series.iloc.__getitem__ = MagicMock(return_value=2.50)
        iv_series = MagicMock()
        iv_series.iloc.__getitem__ = MagicMock(return_value=0.25)
        contract.__getitem__ = MagicMock(side_effect=lambda k: {
            'lastPrice': last_price_series,
            'impliedVolatility': iv_series,
        }[k])

        chain = MagicMock()
        puts_df = MagicMock()
        puts_df.__getitem__ = MagicMock(return_value=contract)
        chain.puts = puts_df
        chain.calls = MagicMock()
        sym_ticker.option_chain.return_value = chain

        def ticker_factory(sym):
            if sym == "SPY":
                spy_t = MagicMock()
                spy_t.history.return_value = spy_hist
                return spy_t
            return sym_ticker

        mock_yf_mod.Ticker.side_effect = ticker_factory

        rows = [
            ("AAPL", "put", 140.0, "2025-03-21", 3.50, -1, 0.0),
            ("MSFT", "put", 300.0, "2025-03-21", 5.00, -1, 0.0),
        ]

        result = check_portfolio_status_logic(rows, user_capital=50000)

        # _analyze_correlation 被呼叫，且傳入包含兩個 symbol 的 dict
        mock_corr.assert_called_once()
        positions_dict = mock_corr.call_args[0][0]
        self.assertIn("AAPL", positions_dict)
        self.assertIn("MSFT", positions_dict)

        # 結果包含 correlation 行
        self.assertIn("corr_line", result)

    @patch('market_analysis.portfolio._analyze_correlation', return_value=[])
    @patch('market_analysis.portfolio._calculate_macro_risk', return_value=["macro_line"])
    @patch('market_analysis.portfolio._evaluate_defense_status', return_value="⏳ 繼續持有")
    @patch('market_analysis.portfolio.gamma', return_value=0.05)
    @patch('market_analysis.portfolio.theta', return_value=-0.02)
    @patch('market_analysis.portfolio.delta', return_value=0.55)
    @patch('market_analysis.portfolio.datetime')
    @patch('market_analysis.portfolio.yf')
    def test_long_call_no_margin_used(self, mock_yf_mod, mock_dt,
                                       mock_delta, mock_theta, mock_gamma,
                                       mock_defense, mock_macro, mock_corr):
        """買方部位 (quantity > 0) → 不應佔用保證金"""
        mock_dt.now.return_value = datetime(2025, 1, 15)
        mock_dt.strptime = datetime.strptime

        spy_hist = MagicMock()
        spy_close = MagicMock()
        spy_close.iloc.__getitem__ = MagicMock(return_value=500.0)
        spy_hist.__getitem__ = MagicMock(return_value=spy_close)
        spy_hist.empty = False

        sym_ticker = MagicMock()
        sym_hist = MagicMock()
        sym_hist.empty = False
        sym_close = MagicMock()
        sym_close.iloc.__getitem__ = MagicMock(return_value=150.0)
        sym_hist.__getitem__ = MagicMock(return_value=sym_close)
        sym_ticker.history.return_value = sym_hist
        sym_ticker.info = {'beta': 1.0, 'dividendYield': 0.01}

        contract = MagicMock()
        contract.empty = False
        last_price_series = MagicMock()
        last_price_series.iloc.__getitem__ = MagicMock(return_value=5.00)
        iv_series = MagicMock()
        iv_series.iloc.__getitem__ = MagicMock(return_value=0.30)
        contract.__getitem__ = MagicMock(side_effect=lambda k: {
            'lastPrice': last_price_series,
            'impliedVolatility': iv_series,
        }[k])

        chain = MagicMock()
        calls_df = MagicMock()
        calls_df.__getitem__ = MagicMock(return_value=contract)
        chain.calls = calls_df
        chain.puts = MagicMock()
        sym_ticker.option_chain.return_value = chain

        def ticker_factory(sym):
            if sym == "SPY":
                spy_t = MagicMock()
                spy_t.history.return_value = spy_hist
                return spy_t
            return sym_ticker

        mock_yf_mod.Ticker.side_effect = ticker_factory

        rows = [("AAPL", "call", 160.0, "2025-03-21", 3.00, 1, 0.0)]
        check_portfolio_status_logic(rows, user_capital=50000)

        # 買方部位的 margin 應為 0
        call_args = mock_macro.call_args
        actual_margin = call_args[0][2]
        self.assertAlmostEqual(actual_margin, 0.0, places=2)

    @patch('market_analysis.portfolio._analyze_correlation', return_value=[])
    @patch('market_analysis.portfolio._calculate_macro_risk', return_value=["macro_line"])
    @patch('market_analysis.portfolio.gamma')
    @patch('market_analysis.portfolio.theta')
    @patch('market_analysis.portfolio.delta')
    @patch('market_analysis.portfolio.datetime')
    @patch('market_analysis.portfolio.yf')
    def test_greeks_exception_uses_zero(self, mock_yf_mod, mock_dt,
                                         mock_delta, mock_theta, mock_gamma,
                                         mock_macro, mock_corr):
        """Greeks 計算拋出異常時，使用 (0, 0, 0) fallback"""
        mock_dt.now.return_value = datetime(2025, 1, 15)
        mock_dt.strptime = datetime.strptime

        spy_hist = MagicMock()
        spy_close = MagicMock()
        spy_close.iloc.__getitem__ = MagicMock(return_value=500.0)
        spy_hist.__getitem__ = MagicMock(return_value=spy_close)
        spy_hist.empty = False

        sym_ticker = MagicMock()
        sym_hist = MagicMock()
        sym_hist.empty = False
        sym_close = MagicMock()
        sym_close.iloc.__getitem__ = MagicMock(return_value=150.0)
        sym_hist.__getitem__ = MagicMock(return_value=sym_close)
        sym_ticker.history.return_value = sym_hist
        sym_ticker.info = {'beta': 1.0, 'dividendYield': 0.01}

        contract = MagicMock()
        contract.empty = False
        last_price_series = MagicMock()
        last_price_series.iloc.__getitem__ = MagicMock(return_value=2.50)
        iv_series = MagicMock()
        iv_series.iloc.__getitem__ = MagicMock(return_value=0.25)
        contract.__getitem__ = MagicMock(side_effect=lambda k: {
            'lastPrice': last_price_series,
            'impliedVolatility': iv_series,
        }[k])

        chain = MagicMock()
        puts_df = MagicMock()
        puts_df.__getitem__ = MagicMock(return_value=contract)
        chain.puts = puts_df
        chain.calls = MagicMock()
        sym_ticker.option_chain.return_value = chain

        def ticker_factory(sym):
            if sym == "SPY":
                spy_t = MagicMock()
                spy_t.history.return_value = spy_hist
                return spy_t
            return sym_ticker

        mock_yf_mod.Ticker.side_effect = ticker_factory

        # 讓 Greeks 拋出異常
        mock_delta.side_effect = ValueError("bad iv")
        mock_theta.side_effect = ValueError("bad iv")
        mock_gamma.side_effect = ValueError("bad iv")

        rows = [("AAPL", "put", 140.0, "2025-03-21", 3.50, -1, 0.0)]

        # 不應拋出異常
        result = check_portfolio_status_logic(rows, user_capital=50000)
        self.assertIsInstance(result, list)


if __name__ == '__main__':
    unittest.main()
