import unittest
import pandas as pd

class TestRiskEngine(unittest.TestCase):
    def setUp(self):
        """模擬測試環境參數"""
        self.user_capital = 50000.0
        self.risk_limit_pct = 15.0
        self.spy_price = 500.0
        # 假設目前已有正向曝險 (持有 TSLA/PLTR)
        self.current_total_weighted_delta = 10.0 

    def test_high_beta_exposure_alert(self):
        """測試：高 Beta 標的 (LMND) 是否會正確觸發風控警報"""
        
        # 1. 模擬 LMND 掃描結果
        scan_result = {
            "symbol": "LMND",
            "beta": 2.45,
            "weighted_delta": -25.48,  # 單口 Delta * Beta * (S/Spy)
            "margin_per_contract": 4500.0
        }
        
        # 2. 模擬計算建議口數 (假設策略分配 5% 資金)
        alloc_pct = 0.05
        suggested_contracts = int((self.user_capital * min(alloc_pct, 0.25)) // scan_result["margin_per_contract"])
        if suggested_contracts == 0: suggested_contracts = 1

        # 3. 模擬 What-if 衝擊 (賣出 Put = 負 Delta * 數量 * -1 = 正向曝險)
        new_trade_impact = scan_result["weighted_delta"] * suggested_contracts * -1
        projected_total_delta = self.current_total_weighted_delta + new_trade_impact
        
        # 4. 計算曝險比例
        projected_exposure_pct = (projected_total_delta * self.spy_price / self.user_capital) * 100
        
        # 輸出結果供觀察
        print(f"\n[Test Result] {scan_result['symbol']} 模擬曝險: {projected_exposure_pct:.1f}%")
        
        # 5. 斷言測試：由於 LMND Beta 極高，預期會衝破 15% 紅線
        self.assertGreater(projected_exposure_pct, self.risk_limit_pct, 
                           f"LMND 交易應觸發紅線警報，但結果為 {projected_exposure_pct:.1f}%")

if __name__ == "__main__":
    unittest.main()