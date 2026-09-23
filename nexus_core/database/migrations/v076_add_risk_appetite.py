version = 76
description = "新增 user_settings.risk_appetite 欄位，供 /settings 選擇風險偏好 (DEFENSIVE/AGGRESSIVE)"
sql = """
ALTER TABLE user_settings ADD COLUMN risk_appetite TEXT DEFAULT 'DEFENSIVE';
"""
