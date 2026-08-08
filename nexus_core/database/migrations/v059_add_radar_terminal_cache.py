version = 59
description = "Add radar_terminal_cache table for fast-track radar scanning"
sql = """
CREATE TABLE IF NOT EXISTS radar_terminal_cache (
    symbol TEXT PRIMARY KEY,
    put_wall_strike REAL,
    mp_near REAL,
    mp_far REAL,
    is_divergence BOOLEAN,
    is_skew_extreme BOOLEAN,
    hvn_price REAL,
    lvn_price REAL,
    avg_vol_20d REAL,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
"""
