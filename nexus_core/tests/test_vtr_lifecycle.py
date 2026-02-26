import unittest
from datetime import datetime, timedelta
from unittest.mock import patch

from market_analysis.ghost_trader import GhostTrader

class TestVTRLifecycle(unittest.TestCase):
    def setUp(self):
        self.user_id = 999
        self.symbol = "AAPL"

    @patch('market_analysis.ghost_trader.GhostTrader._find_target_contract')
    @patch('market_analysis.ghost_trader.delta')
    @patch('market_analysis.ghost_trader.GhostTrader.get_option_mid_price')
    @patch('market_analysis.ghost_trader.market_data_service.get_quote')
    @patch('market_analysis.ghost_trader.get_all_open_virtual_trades')
    @patch('market_analysis.ghost_trader.close_virtual_trade')
    @patch('market_analysis.ghost_trader.add_virtual_trade')
    def test_rolling_to_new_contract_chain(
        self, mock_add, mock_close, mock_get_open, mock_quote, 
        mock_mid, mock_delta, mock_find
    ):
        """
        [整合測試] 模擬：部位虧損 -> 觸發轉倉 -> 產生新 ID -> PnL 歸檔
        """
        # 1. 模擬資料庫中有一筆 AAPL 賣 Put
        mock_get_open.return_value = [{
            'id': 50, 'user_id': self.user_id, 'symbol': 'AAPL', 
            'opt_type': 'put', 'strike': 200.0, 'entry_price': 5.0, 
            'quantity': -1, 'status': 'OPEN', 'expiry': '2026-03-20'
        }]

        # 2. 模擬市場暴跌：現價 $180, 該 Put Delta 變成 -0.65, Mid 價漲到 22.0
        mock_quote.return_value = {'c': 180.0}
        mock_mid.return_value = (22.0, 0.5) # mid, iv
        mock_delta.return_value = -0.65

        # 3. 模擬找到的新轉倉目標 ($170 Put, 45 DTE)
        mock_find.return_value = {
            'strike': 170.0, 'expiry': '2026-04-17'
        }

        # 設定 mock_add 的回傳值（因為 ghost_trader.py 沒有去接，但安全起見給個整數）
        mock_add.return_value = 51

        # 4. 執行 GhostTrader 的監控任務
        gt = GhostTrader()
        gt.execute_virtual_roll()

        # --- 驗證邏輯 ---
        
        # 驗證 A: 舊部位 (ID 50) 是否被平倉 (close_virtual_trade 被調用)
        self.assertTrue(mock_close.called)
        args, kwargs = mock_close.call_args
        self.assertEqual(args[0], 50) # trade_id
        # exit_price = mid * 1.01 = 22.0 * 1.01 = 22.22
        self.assertAlmostEqual(args[1], 22.22)
        self.assertEqual(kwargs['status'], 'ROLLED')
        print(f"✅ 舊部位結算成功: Exit {args[1]}")

        # 驗證 B: 是否建立了關聯的新部位
        self.assertTrue(mock_add.called)
        new_trade_kwargs = mock_add.call_args[1]
        self.assertEqual(new_trade_kwargs['parent_trade_id'], 50)
        self.assertEqual(new_trade_kwargs['strike'], 170.0)
        self.assertEqual(new_trade_kwargs['tags'], ["rolled_from:50"])
        print(f"✅ 新轉倉部位鏈接成功: Parent ID {new_trade_kwargs['parent_trade_id']}")

if __name__ == '__main__':
    unittest.main()