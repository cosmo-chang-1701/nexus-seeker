import sqlite3

version = 34
description = (
    "Add option_alert_mode for three-stage alert filtering (OFF/ALL/PORTFOLIO_ONLY)"
)

sql = """
ALTER TABLE user_settings ADD COLUMN option_alert_mode INTEGER DEFAULT 1;

-- Initialize option_alert_mode based on existing enable_option_alerts
UPDATE user_settings SET option_alert_mode = 0 WHERE enable_option_alerts = 0;
UPDATE user_settings SET option_alert_mode = 1 WHERE enable_option_alerts = 1;
"""


def migrate_data(conn: sqlite3.Connection):
    pass
