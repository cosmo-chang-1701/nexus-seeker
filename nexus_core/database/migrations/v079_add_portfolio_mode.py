version = 79
description = "新增 user_settings.portfolio_mode 欄位，供 /settings 選擇持倉管理模式 (COMMAND/ADVISORY)"
sql = """
ALTER TABLE user_settings ADD COLUMN portfolio_mode TEXT DEFAULT 'COMMAND';
"""
