version = 44
description = "Add market_cache table and enable_local_tunnel to user_settings"
sql = """
CREATE TABLE IF NOT EXISTS market_cache (
    symbol TEXT PRIMARY KEY,
    max_pain REAL,
    expected_move_lower REAL,
    expected_move_upper REAL,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
ALTER TABLE user_settings ADD COLUMN enable_local_tunnel BOOLEAN DEFAULT 0;
"""
