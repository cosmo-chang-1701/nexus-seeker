version = 16
description = "新增使用者偏好設定開關 (enable_option_alerts, enable_vtr, enable_psq_watchlist)"
sql = """
    ALTER TABLE user_settings ADD COLUMN enable_option_alerts BOOLEAN DEFAULT 1;
    ALTER TABLE user_settings ADD COLUMN enable_vtr BOOLEAN DEFAULT 1;
    ALTER TABLE user_settings ADD COLUMN enable_psq_watchlist BOOLEAN DEFAULT 0;
"""
