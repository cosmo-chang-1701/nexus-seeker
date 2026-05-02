version = 18
description = "Add polymarket_threshold for whale monitoring"

sql = """
ALTER TABLE user_settings ADD COLUMN polymarket_threshold REAL DEFAULT 10000.0;
"""
