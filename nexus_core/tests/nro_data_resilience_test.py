import unittest
from market_analysis.portfolio import safe_get_price, safe_get_dividend # 假設您放在這

class MockFastInfo:
    def __init__(self, data):
        for k, v in data.items(): setattr(self, k, v)
    def __getattr__(self, name):
        raise AttributeError(f"MockFastInfo no attr: {name}")

class TestDataResilience(unittest.TestCase):
    def test_fast_info_attribute_access(self):
        # 模擬 2026 真實數據物件
        mock = MockFastInfo({'lastPrice': 691.42, 'dividendYield': 0.013})
        
        # 驗證 getattr 防禦邏輯
        self.assertEqual(safe_get_price(mock), 691.42)
        self.assertEqual(safe_get_dividend(mock), 0.013)
        
        # 驗證不存在屬性時的 Fallback
        self.assertEqual(getattr(mock, 'non_exist', 500.0), 500.0)

if __name__ == '__main__':
    unittest.main()