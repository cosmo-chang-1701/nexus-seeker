version = 39
description = "Add user_notification_settings table for granular notification toggles"
sql = """
CREATE TABLE IF NOT EXISTS user_notification_settings (
    user_id INTEGER NOT NULL,
    notification_key TEXT NOT NULL,
    enabled INTEGER NOT NULL DEFAULT 1,
    PRIMARY KEY (user_id, notification_key)
);

CREATE INDEX IF NOT EXISTS idx_user_notification_settings ON user_notification_settings(user_id);
"""
