import unittest
import math
# 這裡導入您實際的函數
# from market_analysis.portfolio import optimize_position_risk

def optimize_position_risk(current_delta, unit_weighted_delta, user_capital, spy_price, strategy, risk_limit_pct=15.0):
    """
    [被測函數複製品] 供測試腳本獨立運行
    """
    max_safe_delta = (user_capital * (risk_limit_pct / 100)) / spy_price
    min_safe_delta = -max_safe_delta
    
    side_multiplier = -1 if "STO" in strategy else 1
    unit_impact = unit_weighted_delta * side_multiplier
    
    if unit_impact == 0: return 0, 0.0

    # 判斷方向並尋找空間
    if unit_impact > 0:
        delta_room = max_safe_delta - current_delta
    else:
        delta_room = min_safe_delta - current_delta

    # 計算安全口數
    if (delta_room > 0 and unit_impact > 0) or (delta_room < 0 and unit_impact < 0):
        safe_qty = int(abs(delta_room) // abs(unit_impact))
    else:
        safe_qty = 0

    # 計算對沖股數
    suggested_hedge_spy = 0.0
    if safe_qty == 0:
        projected_with_one = current_delta + unit_impact
        if unit_impact > 0 and projected_with_one > max_safe_delta:
            suggested_hedge_spy = projected_with_one - max_safe_delta
        elif unit_impact < 0 and projected_with_one < min_safe_delta:
            suggested_hedge_spy = projected_with_one - min_safe_delta
            
    return safe_qty, round(abs(suggested_hedge_spy), 2)

class TestNexusRiskOptimizer(unittest.TestCase):
    
    def setUp(self):
        """設定基準參數"""
        self.spy_price = 500.0
        self.capital = 50000.0
        self.limit = 15.0  # 15% 紅線
        # 最大允許 Delta = (50000 * 0.15) / 500 = 15.0 股

    def test_sto_put_direction_and_overload(self):
        """驗證 STO PUT 是否正確識別為多頭衝擊，並在過載時攔截"""
        # 現有 10 股，新單衝擊 +5.62 (STO PUT -0.22 Delta * -1)
        # 總量 15.62 > 15.0
        current_delta = 10.0
        unit_delta = -5.62  # 合約 Delta
        strategy = "STO_PUT"
        
        qty, hedge = optimize_position_risk(
            current_delta, unit_delta, self.capital, self.spy_price, strategy
        )
        
        self.assertEqual(qty, 0, "應攔截過載的多頭 STO_PUT")
        self.assertAlmostEqual(hedge, 0.62, places=2, msg="對沖股數應為 0.62 (15.62 - 15.0)")

    def test_sto_call_hedging_effect(self):
        """驗證 STO CALL (看空) 是否能抵銷現有多頭部位，不觸發警告"""
        # 現有 14.0 股多頭，賣出 Call (-3.0 衝擊)
        # 14.0 - 3.0 = 11.0 (在 15 內)
        current_delta = 14.0
        unit_delta = 3.0  # 合約 Delta (正)
        strategy = "STO_CALL"
        
        qty, hedge = optimize_position_risk(
            current_delta, unit_delta, self.capital, self.spy_price, strategy
        )
        
        self.assertGreaterEqual(qty, 1, "STO_CALL 應被視為減輕風險的動作")
        self.assertEqual(hedge, 0.0)

    def test_short_side_limit_defense(self):
        """驗證空頭防禦死角 (Bug 3 修復檢查)：已大量放空時攔截新空單"""
        # 現有 -14.0 股，再賣出 Call (衝擊 -3.0)
        # 總量 -17.0 < -15.0 (超標)
        current_delta = -14.0
        unit_delta = 3.0
        strategy = "STO_CALL"
        
        qty, hedge = optimize_position_risk(
            current_delta, unit_delta, self.capital, self.spy_price, strategy
        )
        
        self.assertEqual(qty, 0, "應攔截超標的空頭部位")
        self.assertAlmostEqual(hedge, 2.0, places=2, msg="應建議買入 2.0 股 SPY 對沖")

    def test_low_capital_rejection(self):
        """驗證低本金下是否連 1 口都不給過"""
        low_capital = 2000.0  # 限額僅 0.6 股 SPY
        current_delta = 0.0
        unit_delta = -5.62
        strategy = "STO_PUT"
        
        qty, hedge = optimize_position_risk(
            current_delta, unit_delta, low_capital, self.spy_price, strategy
        )
        
        self.assertEqual(qty, 0)
        self.assertGreater(hedge, 5.0)

if __name__ == '__main__':
    unittest.main()