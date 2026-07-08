version = 56
description = "新增 PowerSqueeze 快取 (squeeze_cache) 資料表"
sql = """
CREATE TABLE IF NOT EXISTS squeeze_cache (
    symbol TEXT PRIMARY KEY,
    is_squeezing INTEGER NOT NULL DEFAULT 0,
    momentum REAL NOT NULL DEFAULT 0.0,
    direction TEXT NOT NULL DEFAULT '⚪',
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
"""
