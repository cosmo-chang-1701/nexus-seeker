import unittest
import pandas as pd
import numpy as np

# ==========================================
# 核心邏輯區：防禦性取值模式
# ==========================================

def safe_analyze_logic_mock(symbol_data):
    """
    模擬 analyze_symbol 內部的安全取值邏輯
    避免 'NoneType' object is not subscriptable
    """
    # ❌ 錯誤做法：data['price'] -> 如果 data 是 None 就會崩潰
    # ✅ 正確做法：使用 .get() 並配合早期退出 (Early Return)
    
    if symbol_data is None:
        return None
    
    # 使用 .get() 確保即便 key 不存在也不會報錯，而是拿到預設值
    price = symbol_data.get('price', 0.0)
    strategy = symbol_data.get('strategy', 'WAIT')
    
    # 針對巢狀字典或物件，先檢查是否存在
    best_contract = symbol_data.get('best_contract')
    if best_contract is None:
        # 如果找不到合約，直接優雅退出，不要執行後面的計算
        return None
        
    strike = best_contract.get('strike', 0.0)
    return f"{strategy} @ {strike}"

# ==========================================
# 擴充測試案例：Resilience (韌性測試)
# ==========================================

class TestNROResilience(unittest.TestCase):

    def test_nonetype_data_resilience(self):
        """[Resilience] 驗證當 API 回傳 None 時，系統不會崩潰"""
        # 模擬 TOL 案例：資料源完全斷線回傳 None
        bad_data = None
        
        try:
            result = safe_analyze_logic_mock(bad_data)
            self.assertIsNone(result)
            print("✅ 成功攔截 None 數據，未觸發 Subscriptable 錯誤")
        except TypeError as e:
            self.fail(f"❌ 崩潰！偵測到 NoneType 錯誤: {e}")

    def test_missing_contract_resilience(self):
        """[Resilience] 驗證當標的有價格但『沒合約』時，系統不會崩潰"""
        # 模擬 TOL 案例：有股價，但該履約價合約剛好沒掛牌 (None)
        incomplete_data = {
            'symbol': 'TOL',
            'price': 150.2,
            'strategy': 'STO_PUT',
            'best_contract': None # 這是關鍵崩潰點
        }
        
        result = safe_analyze_logic_mock(incomplete_data)
        self.assertIsNone(result, "當合約缺失時應回傳 None 確保後續 Embed 不會亂噴")
        print("✅ 成功處理『有標的、無合約』的邊際案例")

if __name__ == '__main__':
    # 執行所有測試，包含您之前的 UI 測試
    unittest.main()