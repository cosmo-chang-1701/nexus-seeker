version = 44
description = (
    "Add market_cache table for storing pre-calculated Max Pain and Expected Move data"
)
sql = """
CREATE TABLE IF NOT EXISTS market_cache (
    symbol TEXT PRIMARY KEY,
    max_pain REAL,
    expected_move_lower REAL,
    expected_move_upper REAL,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
"""
