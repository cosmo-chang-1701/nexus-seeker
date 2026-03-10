version = 10
description = "新增自動回補避險時間追蹤，用於通知頻率抑制。"
sql = """
    ALTER TABLE user_settings ADD COLUMN last_rehedge_alert_time INTEGER DEFAULT 0;
"""
