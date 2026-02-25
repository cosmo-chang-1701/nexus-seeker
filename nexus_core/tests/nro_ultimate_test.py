import unittest
import pandas as pd
import numpy as np
import math
import logging

# 封鎖 yfinance 噪音
logging.getLogger('yfinance').setLevel(logging.CRITICAL)

# ==========================================
# 核心邏輯區 (待測函數)
# ==========================================

def calculate_beta(df_stock: pd.DataFrame, df_spy: pd.DataFrame) -> float:
    try:
        if df_stock is None or df_spy is None or df_stock.empty or df_spy.empty:
            return 1.0
        
        # 1. 對齊資料
        combined = pd.merge(
            df_stock['Close'], df_spy['Close'], 
            left_index=True, right_index=True, 
            how='inner', suffixes=('_stock', '_spy')
        ).dropna()
        
        if len(combined) < 10: return 1.0
        
        # 2. 計算收益率
        returns = combined.pct_change().dropna()
        
        # 3. 檢查變異數，避免 0/0
        spy_var = returns['Close_spy'].var()
        if spy_var < 1e-9: return 1.0
        
        # 4. 計算 Beta
        cov = returns['Close_stock'].cov(returns['Close_spy'])
        beta = cov / spy_var
        
        return round(float(np.clip(beta, -5.0, 5.0)), 2)
    except Exception:
        return 1.0

def optimize_position_risk(current_delta, unit_weighted_delta, user_capital, spy_price, strategy, risk_limit_pct=15.0):
    max_safe_delta = (user_capital * (risk_limit_pct / 100)) / spy_price
    min_safe_delta = -max_safe_delta
    side_multiplier = -1 if "STO" in strategy else 1
    unit_impact = unit_weighted_delta * side_multiplier
    
    if unit_impact == 0: return 0, 0.0
    
    # 判定空間
    if unit_impact > 0:
        delta_room = max_safe_delta - current_delta
    else:
        delta_room = min_safe_delta - current_delta

    # 計算安全口數
    if (delta_room > 0 and unit_impact > 0) or (delta_room < 0 and unit_impact < 0):
        if abs(unit_impact) > abs(delta_room):
            safe_qty = 0
        else:
            safe_qty = int(abs(delta_room) // abs(unit_impact))
    else:
        safe_qty = 0

    suggested_hedge_spy = 0.0
    if safe_qty == 0:
        projected_with_one = current_delta + unit_impact
        if unit_impact > 0 and projected_with_one > max_safe_delta:
            suggested_hedge_spy = projected_with_one - max_safe_delta
        elif unit_impact < 0 and projected_with_one < min_safe_delta:
            suggested_hedge_spy = projected_with_one - min_safe_delta
            
    return safe_qty, round(abs(suggested_hedge_spy), 2)

# ==========================================
# 終極測試類別
# ==========================================

class TestNROUltimateSuite(unittest.TestCase):
    
    def setUp(self):
        """🚀 關鍵修正：加入隨機噪音，避免變異數為零"""
        np.random.seed(42) # 鎖定隨機子，確保每次測試結果一致
        self.spy_price = 500.0
        self.capital = 50000.0
        self.limit_delta = 15.0 
        
        dates = pd.date_range(start="2026-01-01", periods=60) # 增加長度至 60 天
        
        # 模擬大盤: 每日回報 0.5% + 隨機波動
        spy_returns = np.random.normal(0.005, 0.001, 60)
        spy_prices = [500.0]
        for r in spy_returns:
            spy_prices.append(spy_prices[-1] * (1 + r))
        self.mock_spy = pd.DataFrame({'Close': spy_prices[:-1]}, index=dates)
        
        # 模擬標的: 漲幅是大盤的 2 倍 (Beta = 2.0) + 隨機波動
        stock_prices = [100.0]
        for r in spy_returns:
            # 標的回報 = 大盤回報 * 2
            stock_prices.append(stock_prices[-1] * (1 + r * 2.0))
        self.mock_stock = pd.DataFrame({'Close': stock_prices[:-1]}, index=dates)

    def test_pipeline_beta_alignment(self):
        """[Pipeline] 驗證 Beta 計算是否準確鎖定在 2.0"""
        beta = calculate_beta(self.mock_stock, self.mock_spy)
        # 預期應精確等於 2.0
        self.assertAlmostEqual(beta, 2.0, delta=0.1)

    def test_pipeline_full_flow(self):
        """[Pipeline] 模擬重倉過載與對沖建議"""
        beta = calculate_beta(self.mock_stock, self.mock_spy) # 2.0
        
        # 模擬極端風險: 單口加權 Delta 衝擊高達 20.0
        # 為了確保觸發對沖，我們手動設定一個會讓總曝險大幅超標的數值
        unit_weighted_delta = -20.0 
        strategy = "STO_PUT" # 衝擊 = +20.0
        
        # 目前已持倉 10.0，加上新單 20.0 = 30.0 (限額 15.0)
        # 超標 15.0 股
        qty, hedge = optimize_position_risk(10.0, unit_weighted_delta, self.capital, self.spy_price, strategy)
        
        self.assertEqual(qty, 0, f"應攔截過載部位，但得到 qty={qty}")
        self.assertGreater(hedge, 10.0, f"對沖股數應反映超標部分，但得到 hedge={hedge}")

if __name__ == '__main__':
    unittest.main()