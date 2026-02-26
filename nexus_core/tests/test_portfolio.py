import unittest
from unittest.mock import MagicMock, patch, PropertyMock
import sys
from types import ModuleType
from datetime import datetime, date

# --- 1. 核心 MOCK：在導入前切斷所有外部依賴 ---
mock_pd = MagicMock()
sys.modules["pandas"] = mock_pd

mock_yf = MagicMock()
mock_yf.__spec__ = None
sys.modules["yfinance"] = mock_yf

mock_market_data_service = MagicMock()
# 設定 2026 年預設基準價
mock_market_data_service.get_quote.return_value = {'c': 690.0, 'd': 1.0, 'dp': 0.15}
sys.modules["services"] = MagicMock()
sys.modules["services.market_data_service"] = mock_market_data_service

# 其他模組 Mock (保持原樣)
sys.modules["pandas_ta"] = MagicMock()
sys.modules["market_analysis.strategy"] = MagicMock()
sys.modules["market_analysis.greeks"] = MagicMock()
sys.modules["market_analysis.data"] = MagicMock()
sys.modules["py_vollib"] = MagicMock()

# Mock config
mock_config = ModuleType("config")
mock_config.FINNHUB_API_KEY = "test_key"
sys.modules["config"] = mock_config

# --- 2. 導入待測函數 ---
from market_analysis.portfolio import (
    check_portfolio_status_logic,
    evaluate_defense_status,
    calculate_macro_risk,
    simulate_exposure_impact,
    optimize_position_risk
)

# ====================================================================
# Orchestrator Tests (已適配 Finnhub 遷移)
# ====================================================================
class TestCheckPortfolioStatusLogic(unittest.TestCase):
    
    def _setup_common_mocks(self, mock_fh, mock_yf_mod):
        """統一配置 Finnhub 與 yfinance 的 Mock 狀態"""
        # 🚀 Finnhub: 負責所有價格與基本面
        mock_fh.get_quote.side_effect = lambda sym: {
            'c': 690.0 if sym == "SPY" else 150.0, 
            'd': 1.0, 'dp': 0.5
        }
        mock_fh.get_history_df.return_value = MagicMock(empty=False)
        mock_fh.get_dividend_yield.return_value = 0.01
        mock_fh.is_etf.return_value = False

        # 🚀 yfinance: 僅負責 Option Chain
        mock_chain = MagicMock()
        mock_contract = MagicMock(empty=False)
        mock_contract.__getitem__ = MagicMock(side_effect=lambda k: MagicMock(iloc=[2.5 if k=='lastPrice' else 0.25]))
        mock_chain.puts = MagicMock(__getitem__=MagicMock(return_value=mock_contract))
        mock_chain.calls = MagicMock(__getitem__=MagicMock(return_value=mock_contract))
        
        mock_ticker = MagicMock()
        mock_ticker.option_chain.return_value = mock_chain
        mock_yf_mod.Ticker.return_value = mock_ticker

    @patch('market_analysis.portfolio.analyze_correlation', return_value=[])
    @patch('market_analysis.portfolio.calculate_macro_risk', return_value=["🛡️ 宏觀風險：健康"])
    @patch('market_analysis.portfolio.evaluate_defense_status', return_value="⏳ 繼續持有")
    @patch('market_analysis.portfolio.gamma', return_value=0.01)
    @patch('market_analysis.portfolio.theta', return_value=0.05)
    @patch('market_analysis.portfolio.delta', return_value=-0.15)
    @patch('market_analysis.portfolio.market_data_service')
    @patch('market_analysis.portfolio.yf')
    def test_single_short_put_flow(self, mock_yf_mod, mock_fh, *args):
        """驗證從 Finnhub 抓取報價到 yfinance 抓取期權的完整流程"""
        self._setup_common_mocks(mock_fh, mock_yf_mod)
        
        # 測試數據
        rows = [("AAPL", "put", 140.0, "2025-03-21", 3.50, -1, 0.0)]
        result = check_portfolio_status_logic(rows, user_capital=50000)

        # 斷言檢測
        report_text = "\n".join(result)
        self.assertIn("AAPL", report_text)
        self.assertIn("繼續持有", report_text)
        
        # 驗證數據來源是否正確切換
        mock_fh.get_quote.assert_any_call("AAPL")
        mock_fh.get_quote.assert_any_call("SPY")
        mock_yf_mod.Ticker.assert_called_with("AAPL")

# ====================================================================
# Risk Optimization Tests (15% 紅線邏輯)
# ====================================================================
class TestRiskOptimization(unittest.TestCase):
    
    def test_exposure_limit_and_hedge(self):
        """驗證當曝險超過 15% 時是否觸發對沖建議"""
        # 資金 5萬, SPY 500, 限額 15% -> Max Delta = 15.0
        # 目前已持 14.0, 擬新增 BTO_CALL (+2.5 Delta)
        # Expected: Qty=0, Hedge=1.5 股 SPY (16.5 - 15.0)
        qty, hedge = optimize_position_risk(
            current_delta=14.0, 
            unit_weighted_delta=2.5, 
            user_capital=50000, 
            spy_price=500, 
            risk_limit_pct=15.0, 
            strategy='BTO_CALL'
        )
        self.assertEqual(qty, 0)
        self.assertEqual(hedge, 1.5)

if __name__ == '__main__':
    unittest.main()