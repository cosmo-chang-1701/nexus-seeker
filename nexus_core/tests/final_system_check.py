import unittest
from unittest.mock import MagicMock, patch
import pandas as pd
import time
from services.market_data_service import get_sma, _sma_cache
from market_analysis.hedging import get_market_regime_target, calculate_autonomous_hedge
from market_analysis.ghost_trader import GhostTrader

class FinalSystemCheck(unittest.TestCase):
    def setUp(self):
        self.uid = 236769512043708418
        self.capital = 100000.0
        _sma_cache.clear() # 每次測試前清空快取

    # 1. 驗證 SMA 快取機制 (性能與正確性)
    @patch('services.market_data_service.get_history_df')
    def test_sma_caching_performance(self, mock_history):
        # 模擬回傳 250 天的收盤價，全都是 500.0
        data = {'Close': [500.0] * 250}
        mock_history.return_value = pd.DataFrame(data)
        
        # 第一次呼叫 (應觸發 Cache Miss)
        val1 = get_sma("SPY", 200)
        self.assertEqual(val1, 500.0)
        self.assertEqual(mock_history.call_count, 1)

        # 第二次呼叫 (應觸發 Cache Hit)
        val2 = get_sma("SPY", 200)
        self.assertEqual(val2, 500.0)
        self.assertEqual(mock_history.call_count, 1) # 呼叫次數仍應為 1
        print("✅ SMA 快取機制驗證通過：重複讀取不消耗 API 配額")

    # 2. 驗證自主對沖判定 (市場位階感知)
    @patch('services.market_data_service.get_sma')
    @patch('services.market_data_service.get_quote')
    def test_autonomous_regime_logic(self, mock_quote, mock_sma):
        # 模擬【牛市】情境：現價 > SMA200 且 VIX 低
        mock_sma.return_value = 500.0
        mock_quote.return_value = {'c': 15.0}
        
        target, regime = get_market_regime_target(520.0, self.capital)
        self.assertIn("Bull", regime)
        self.assertEqual(target, 200.0) # 100k * 0.2%

        # 模擬【熊市】情境：現價 < SMA200
        target_bear, regime_bear = get_market_regime_target(480.0, self.capital)
        self.assertIn("Bear", regime_bear)
        self.assertEqual(target_bear, 0.0) # 強制回歸中性
        print(f"✅ 自主位階判定通過：{regime} -> Target {target}")

    # 3. 驗證對沖建議計算 (Delta Gap)
    def test_hedge_execution_logic(self):
        # 目前總 Delta +450, 系統判定理想目標應為 +200 (牛市)
        # 缺口 = 200 - 450 = -250 -> 應買入 5 口 PUT (假設每口 Delta -0.5)
        hedge = calculate_autonomous_hedge(450.0, 200.0, 520.0)
        self.assertEqual(hedge['gap'], -250.0)
        self.assertIn("BTO PUT 5 口 SPY", hedge['action'])
        print(f"✅ 對沖建議計算通過：{hedge['action']}")

if __name__ == '__main__':
    unittest.main()