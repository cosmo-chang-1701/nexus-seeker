import unittest
from datetime import datetime

def validate_expiry(expiry: str) -> str:
    # 🛡️ Defensive Programming: Validate Expiry Date Format
    try:
        # Only capture the first 10 characters (YYYY-MM-DD) to prevent trailing argument capture
        expiry_clean = expiry.split(' ')[0]
        datetime.strptime(expiry_clean, '%Y-%m-%d')
        return expiry_clean # Standardized format
    except Exception:
        raise ValueError(f"❌ **日期格式錯誤**: `{expiry}`。請確保為 `YYYY-MM-DD` 格式。")

class TestCommandParserFix(unittest.TestCase):
    def test_standard_date(self):
        self.assertEqual(validate_expiry("2026-05-29"), "2026-05-29")
    
    def test_dirty_date(self):
        # The specific case reported by the user
        dirty_input = "2026-05-29 qty:1 cost:39.5 action:STO tag:SPECULATIVE"
        self.assertEqual(validate_expiry(dirty_input), "2026-05-29")
        
    def test_invalid_date(self):
        with self.assertRaises(ValueError):
            validate_expiry("2026-05-32")
        with self.assertRaises(ValueError):
            validate_expiry("not-a-date")

if __name__ == '__main__':
    unittest.main()
