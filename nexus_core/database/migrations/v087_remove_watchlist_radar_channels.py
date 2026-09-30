version = 87
description = (
    "移除已下線的通知頻道 heartbeat_watchlist（15 分鐘自選雷達）與 "
    "intel_market_scenario（自選股市場情境事件）的使用者設定列"
)

# 兩個頻道的推播來源（cogs/trading/heartbeat.py、market_analysis/scenario_classifier.py）
# 已移除，註冊表也不再列出這兩個 key。殘留列不會被讀取（_merge_rows 會略過未知 key），
# 但保留只會讓 user_notification_settings 帶著無意義的資料，故一次清掉。
# 刻意寫死 key 字面值：遷移必須是當下的凍結快照，不從註冊表匯入。
sql = """
DELETE FROM user_notification_settings
WHERE notification_key IN ('heartbeat_watchlist', 'intel_market_scenario');
"""
