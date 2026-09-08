version = 68
description = "新增 user_settings.trading_strategy 欄位，供 /settings 選擇交易策略模式 (動態調整/左側交易/右側交易)"
sql = """
ALTER TABLE user_settings ADD COLUMN trading_strategy TEXT DEFAULT 'RIGHT_SIDE';
"""
