
version = 13
description = "Add financials_cache table for market data caching"
sql = """
CREATE TABLE IF NOT EXISTS financials_cache (
    symbol TEXT PRIMARY KEY,
    data TEXT NOT NULL,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_financials_updated
ON financials_cache(updated_at);
"""
