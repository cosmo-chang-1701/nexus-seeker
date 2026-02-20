"""
tests/test_check_portfolio_status.py

針對 market_analysis.portfolio.check_portfolio_status_logic 的四個測試案例:
1. test_short_call_generates_report       — 賣 Call 部位應產生包含標的名稱的報告
2. test_zero_entry_price_pnl_is_zero      — entry_price = 0 時 pnl_pct 應為 0
3. test_expired_option_no_crash            — DTE ≤ 0 的已到期合約不應崩潰
4. test_short_call_margin_uses_stock_price — 賣 Call 的保證金應以股價計算
"""

import unittest
from unittest.mock import MagicMock, patch
import sys
from types import ModuleType
from datetime import datetime

# --- MOCK DEPENDENCIES BEFORE IMPORTING PORTFOLIO ---

mock_pd = MagicMock()
sys.modules.setdefault("pandas", mock_pd)

mock_yf = MagicMock()
mock_yf.__spec__ = None
sys.modules.setdefault("yfinance", mock_yf)

# Mock pandas_ta
mock_pandas_ta = MagicMock()
sys.modules.setdefault("pandas_ta", mock_pandas_ta)

mock_greeks_mod = MagicMock()
sys.modules.setdefault("market_analysis.greeks", mock_greeks_mod)
mock_data_mod = MagicMock()
sys.modules.setdefault("market_analysis.data", mock_data_mod)

mock_vollib = MagicMock()
sys.modules.setdefault("py_vollib", mock_vollib)
sys.modules.setdefault("py_vollib.black_scholes_merton", mock_vollib)
sys.modules.setdefault("py_vollib.black_scholes_merton.greeks", mock_vollib)
sys.modules.setdefault("py_vollib.black_scholes_merton.greeks.analytical", mock_vollib)

if "config" not in sys.modules:
    mock_config = ModuleType("config")
    mock_config.RISK_FREE_RATE = 0.042
    sys.modules["config"] = mock_config

import os
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))
from market_analysis.portfolio import check_portfolio_status_logic


# ====================================================================
# Helper: 建構通用 mock 環境
# ====================================================================
def _setup_common_mocks(mock_yf_mod, mock_dt, stock_price=150.0,
                        option_last_price=2.50, option_iv=0.25,
                        spy_price=500.0, now_date=None):
    """建構 yf.Ticker 與 datetime 的通用 mock 設定"""
    if now_date is None:
        now_date = datetime(2025, 1, 15)
    mock_dt.now.return_value = now_date
    mock_dt.strptime = datetime.strptime

    # SPY ticker
    spy_hist = MagicMock()
    spy_close = MagicMock()
    spy_close.iloc.__getitem__ = MagicMock(return_value=spy_price)
    spy_hist.__getitem__ = MagicMock(return_value=spy_close)
    spy_hist.empty = False

    # Symbol ticker
    sym_ticker = MagicMock()
    sym_hist = MagicMock()
    sym_hist.empty = False
    sym_close = MagicMock()
    sym_close.iloc.__getitem__ = MagicMock(return_value=stock_price)
    sym_hist.__getitem__ = MagicMock(return_value=sym_close)
    sym_ticker.history.return_value = sym_hist
    sym_ticker.info = {'beta': 1.0, 'dividendYield': 0.01}

    # option chain contract
    contract = MagicMock()
    contract.empty = False
    last_price_series = MagicMock()
    last_price_series.iloc.__getitem__ = MagicMock(return_value=option_last_price)
    iv_series = MagicMock()
    iv_series.iloc.__getitem__ = MagicMock(return_value=option_iv)
    contract.__getitem__ = MagicMock(side_effect=lambda k: {
        'lastPrice': last_price_series,
        'impliedVolatility': iv_series,
    }[k])

    chain = MagicMock()
    calls_df = MagicMock()
    calls_df.__getitem__ = MagicMock(return_value=contract)
    puts_df = MagicMock()
    puts_df.__getitem__ = MagicMock(return_value=contract)
    chain.calls = calls_df
    chain.puts = puts_df
    sym_ticker.option_chain.return_value = chain

    def ticker_factory(sym):
        if sym == "SPY":
            spy_t = MagicMock()
            spy_t.history.return_value = spy_hist
            return spy_t
        return sym_ticker

    mock_yf_mod.Ticker.side_effect = ticker_factory
    return sym_ticker


# ====================================================================
# Test Cases
# ====================================================================
class TestCheckPortfolioStatusLogicNew(unittest.TestCase):
    """針對 check_portfolio_status_logic 新增的四個測試案例"""

    # ----------------------------------------------------------------
    # Test 1: 賣 Call 部位應產生包含標的名稱的報告
    # ----------------------------------------------------------------
    @patch('market_analysis.portfolio._analyze_correlation', return_value=[])
    @patch('market_analysis.portfolio._calculate_macro_risk', return_value=["macro_line"])
    @patch('market_analysis.portfolio._evaluate_defense_status', return_value="⏳ 繼續持有")
    @patch('market_analysis.portfolio.gamma', return_value=0.03)
    @patch('market_analysis.portfolio.theta', return_value=-0.01)
    @patch('market_analysis.portfolio.delta', return_value=0.30)
    @patch('market_analysis.portfolio.datetime')
    @patch('market_analysis.portfolio.yf')
    def test_short_call_generates_report(self, mock_yf_mod, mock_dt,
                                          mock_delta, mock_theta, mock_gamma,
                                          mock_defense, mock_macro, mock_corr):
        """賣 Call 部位 → 報告行中應包含該標的名稱與 CALL 字樣"""
        _setup_common_mocks(mock_yf_mod, mock_dt, stock_price=180.0,
                            option_last_price=4.00, option_iv=0.30)

        rows = [("TSLA", "call", 200.0, "2025-03-21", 5.00, -1, 0.0)]
        result = check_portfolio_status_logic(rows, user_capital=50000)

        report_text = "\n".join(result)
        self.assertIn("TSLA", report_text)
        self.assertIn("CALL", report_text)
        mock_defense.assert_called_once()
        mock_macro.assert_called_once()

    # ----------------------------------------------------------------
    # Test 2: entry_price = 0 時 pnl_pct 應為 0 (不應 ZeroDivisionError)
    # ----------------------------------------------------------------
    @patch('market_analysis.portfolio._analyze_correlation', return_value=[])
    @patch('market_analysis.portfolio._calculate_macro_risk', return_value=["macro_line"])
    @patch('market_analysis.portfolio._evaluate_defense_status', return_value="⏳ 繼續持有")
    @patch('market_analysis.portfolio.gamma', return_value=0.05)
    @patch('market_analysis.portfolio.theta', return_value=-0.02)
    @patch('market_analysis.portfolio.delta', return_value=-0.15)
    @patch('market_analysis.portfolio.datetime')
    @patch('market_analysis.portfolio.yf')
    def test_zero_entry_price_pnl_is_zero(self, mock_yf_mod, mock_dt,
                                           mock_delta, mock_theta, mock_gamma,
                                           mock_defense, mock_macro, mock_corr):
        """entry_price = 0 → 不應產生除零錯誤，且 pnl_pct 應為 0"""
        _setup_common_mocks(mock_yf_mod, mock_dt)

        rows = [("AAPL", "put", 140.0, "2025-03-21", 0.0, -1, 0.0)]
        result = check_portfolio_status_logic(rows, user_capital=50000)

        report_text = "\n".join(result)
        self.assertIn("AAPL", report_text)
        # pnl_pct = 0.0 → 損益應顯示 +0.00%
        self.assertIn("+0.00%", report_text)
        # _evaluate_defense_status 應接收 pnl_pct = 0.0
        call_args = mock_defense.call_args[0]
        self.assertAlmostEqual(call_args[2], 0.0, places=5)

    # ----------------------------------------------------------------
    # Test 3: 已到期合約 (DTE ≤ 0) 不應崩潰
    # ----------------------------------------------------------------
    @patch('market_analysis.portfolio._analyze_correlation', return_value=[])
    @patch('market_analysis.portfolio._calculate_macro_risk', return_value=["macro_line"])
    @patch('market_analysis.portfolio._evaluate_defense_status', return_value="⏳ 繼續持有")
    @patch('market_analysis.portfolio.gamma', return_value=0.05)
    @patch('market_analysis.portfolio.theta', return_value=-0.02)
    @patch('market_analysis.portfolio.delta', return_value=-0.15)
    @patch('market_analysis.portfolio.datetime')
    @patch('market_analysis.portfolio.yf')
    def test_expired_option_no_crash(self, mock_yf_mod, mock_dt,
                                      mock_delta, mock_theta, mock_gamma,
                                      mock_defense, mock_macro, mock_corr):
        """到期日已過 (DTE ≤ 0) → 不應拋出異常，且 t_years 被 max(dte,1) 保護"""
        # 設定 now = 2025-04-01，但合約到期 = 2025-03-21 → DTE 為負
        _setup_common_mocks(mock_yf_mod, mock_dt,
                            now_date=datetime(2025, 4, 1))

        rows = [("NVDA", "put", 800.0, "2025-03-21", 10.00, -1, 0.0)]
        result = check_portfolio_status_logic(rows, user_capital=100000)

        self.assertIsInstance(result, list)
        report_text = "\n".join(result)
        self.assertIn("NVDA", report_text)
        # Greeks 函式仍應被呼叫 (t_years = max(dte,1)/365 = 1/365)
        mock_delta.assert_called_once()

    # ----------------------------------------------------------------
    # Test 4: Covered Call 的保證金應以持有現股的市值計算
    # ----------------------------------------------------------------
    @patch('market_analysis.portfolio._analyze_correlation', return_value=[])
    @patch('market_analysis.portfolio._calculate_macro_risk', return_value=["macro_line"])
    @patch('market_analysis.portfolio._evaluate_defense_status', return_value="⏳ 繼續持有")
    @patch('market_analysis.portfolio.gamma', return_value=0.03)
    @patch('market_analysis.portfolio.theta', return_value=-0.01)
    @patch('market_analysis.portfolio.delta', return_value=0.30)
    @patch('market_analysis.portfolio.datetime')
    @patch('market_analysis.portfolio.yf')
    def test_covered_call_margin_uses_stock_price(self, mock_yf_mod, mock_dt,
                                                  mock_delta, mock_theta, mock_gamma,
                                                  mock_defense, mock_macro, mock_corr):
        """Covered Call 的 margin = current_stock_price * 100 * abs(qty)"""
        stock_price = 180.0
        _setup_common_mocks(mock_yf_mod, mock_dt, stock_price=stock_price)

        strike = 200.0
        qty = -3
        rows = [("TSLA", "call", strike, "2025-03-21", 5.00, qty, stock_price)]
        check_portfolio_status_logic(rows, user_capital=100000)

        # Covered Call 保證金 = stock_price * 100 * abs(qty) = 180 * 100 * 3 = 54000
        expected_margin = stock_price * 100 * abs(qty)
        call_args = mock_macro.call_args[0]
        actual_margin = call_args[2]  # 第三個位置引數 = total_margin_used
        self.assertAlmostEqual(actual_margin, expected_margin, places=2)


if __name__ == '__main__':
    unittest.main()
