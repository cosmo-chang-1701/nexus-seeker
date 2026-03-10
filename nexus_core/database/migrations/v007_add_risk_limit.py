version = 7
description = "為使用者表新增風險限制欄位，預設值設為 15.0%"
sql = """
    ALTER TABLE user_settings ADD COLUMN risk_limit_pct REAL DEFAULT 15.0;
"""
