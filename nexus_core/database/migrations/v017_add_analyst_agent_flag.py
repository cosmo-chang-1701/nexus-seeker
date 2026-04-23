version = 17
description = "Add enable_analyst_agent column to user_settings"
sql = """
ALTER TABLE user_settings ADD COLUMN enable_analyst_agent BOOLEAN DEFAULT 0;
"""
