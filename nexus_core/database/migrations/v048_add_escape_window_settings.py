version = 48
description = (
    "Add escape_window_start and escape_window_end columns to user_settings table"
)
sql = """
ALTER TABLE user_settings ADD COLUMN escape_window_start TEXT DEFAULT '07-15';
ALTER TABLE user_settings ADD COLUMN escape_window_end TEXT DEFAULT '07-31';
"""
