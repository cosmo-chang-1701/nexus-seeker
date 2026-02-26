import unittest
from unittest.mock import MagicMock, patch
import sys
import os
from datetime import datetime

# 修正路徑確保能抓到 market_analysis
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

class TestVirtualTradingRoom(unittest.TestCase):
    def setUp(self):
        """設定 Mock 環境"""
        self.user_id = 123456789
        self.symbol = "AAPL"

    @patch('database.virtual_trading.add_virtual_trade')
    def test_auto_entry_logic(self, mock_add):
        """測試：當訊號觸發時，是否正確執行模擬建倉 (含 1% 滑點)"""
        from market_analysis.ghost_trader import GhostTrader
        
        scan_data = {
            'symbol': 'AAPL',
            'opt_type': 'put',
            'strike': 150.0,
            'expiry': '2026-03-20',
            'mid_price': 5.0,
            'quantity': -1,
            'suggested_contracts': 1
        }
        
        trader = GhostTrader()
        with patch.object(trader, 'get_option_mid_price', return_value=(5.0, 0.2)):
            trader.record_virtual_entry(
                user_id=self.user_id,
                symbol=scan_data['symbol'],
                opt_type=scan_data['opt_type'],
                strike=scan_data['strike'],
                expiry=scan_data['expiry'],
                quantity=scan_data['quantity']
            )
        
        # 驗證建倉價格是否考慮了賣方的 1% 滑點 (5.0 * 0.99 = 4.95)
        args, kwargs = mock_add.call_args
        self.assertAlmostEqual(kwargs['entry_price'], 4.95)
        print("✅ 自動建倉與滑點模擬測試通過")

    @patch('market_analysis.strategy.get_option_metrics')
    @patch('market_analysis.strategy.find_best_contract')
    @patch('database.virtual_trading.get_virtual_trade_by_id')
    @patch('database.virtual_trading.close_virtual_trade')
    @patch('database.virtual_trading.add_virtual_trade')
    def test_auto_rolling_trigger(self, mock_add, mock_close, mock_get_trade, mock_find, mock_metrics):
        """測試：當 Delta 擴張至 -0.45 時，是否觸發自動轉倉"""
        from market_analysis.ghost_trader import GhostTrader
        
        # 1. 模擬一個已經存在的賣 Put 部位
        mock_get_trade.return_value = {
            'id': 1, 'user_id': self.user_id, 'symbol': 'AAPL', 'opt_type': 'put',
            'strike': 160.0, 'expiry': '2026-03-20', 'entry_price': 5.0, 'quantity': -1, 'status': 'OPEN'
        }
        
        # 2. 模擬當前市場狀況：Delta 已經跌到 -0.45 (觸發紅線)
        mock_metrics.return_value = {'delta': -0.45, 'dte': 30, 'mid': 8.0}
        
        # 3. 模擬尋找到的新合約 (轉倉目標)
        mock_find.return_value = {'strike': 150.0, 'expiry': '2026-04-17', 'mid': 4.5}

        # 4. 執行轉倉檢查
        import asyncio
        trader = GhostTrader()
        with patch('market_analysis.portfolio.market_data_service.get_quote', return_value={'c': 155.0}):
            asyncio.run(trader.check_and_execute_rolling(1))

        # 驗證：舊部位是否被關閉，新部位是否被建立
        mock_close.assert_called_once()
        self.assertEqual(mock_add.call_args[1]['symbol'], 'AAPL')
        self.assertEqual(mock_add.call_args[1]['parent_trade_id'], 1)
        print("✅ Delta 擴張自動轉倉邏輯測試通過")

if __name__ == '__main__':
    unittest.main()
