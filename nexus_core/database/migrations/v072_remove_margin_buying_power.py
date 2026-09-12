version = 72
description = (
    "Remove deprecated option_buying_power and margin_used columns from user_settings"
)
sql = """
ALTER TABLE user_settings DROP COLUMN option_buying_power;
ALTER TABLE user_settings DROP COLUMN margin_used;
"""
