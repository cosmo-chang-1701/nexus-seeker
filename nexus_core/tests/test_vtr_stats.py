import unittest
from datetime import datetime, timedelta
from unittest.mock import MagicMock, patch
import sys
import os

# 確保路徑
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

class TestVTRStatistics(unittest.TestCase):
    def setUp(self):
        self.user_id = 888

    @patch('market_analysis.ghost_trader.get_all_virtual_trades')
    def test_stats_calculation_logic(self, mock_get_all):
        """
        [驗證] 測試 VTR 績效報告的數學邏輯：
        1. 賣 Put 獲利 50% (獲利)
        2. 賣 Put 被迫轉倉結算虧損 (虧損)
        """
        # 模擬兩筆歷史資料
        mock_get_all.return_value = [
            # 獲利單：Entry 5.0, Exit 2.5 (獲利 50%)
            {'status': 'CLOSED', 'entry_price': 5.0, 'exit_price': 2.5, 'quantity': -1, 'pnl': 250.0},
            # 轉倉虧損單：Entry 4.0, Exit 12.0 (虧損)
            {'status': 'ROLLED', 'entry_price': 4.0, 'exit_price': 12.0, 'quantity': -1, 'pnl': -800.0}
        ]

        from market_analysis.ghost_trader import GhostTrader
        stats = GhostTrader.get_vtr_performance_stats(self.user_id)

        # 斷言驗證
        self.assertEqual(stats['total_trades'], 2)
        self.assertEqual(stats['win_rate'], 50.0) # 1 贏 1 輸 = 50%
        self.assertEqual(stats['total_pnl'], -550.0) # 250 - 800 = -550
        print(f"✅ VTR 統計邏輯測試通過: WinRate {stats['win_rate']}% | PnL {stats['total_pnl']}")

if __name__ == '__main__':
    unittest.main()