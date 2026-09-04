version = 67
description = (
    "新增 option_buying_power / margin_used 欄位，供使用者自填保證金購買力參考數據"
)
sql = """
ALTER TABLE user_settings ADD COLUMN option_buying_power REAL DEFAULT 0.0;
ALTER TABLE user_settings ADD COLUMN margin_used REAL DEFAULT 0.0;
"""
