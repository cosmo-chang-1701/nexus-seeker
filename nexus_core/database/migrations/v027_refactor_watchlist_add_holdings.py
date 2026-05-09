version = 27
description = "Refactor watchlist (remove stock_cost) and add independent holdings table with weighted_delta"
sql = """
-- 1. Create independent holdings table
CREATE TABLE IF NOT EXISTS holdings (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL,
    symbol TEXT NOT NULL,
    quantity REAL DEFAULT 0.0,
    avg_cost REAL DEFAULT 0.0,
    weighted_delta REAL DEFAULT 0.0,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_holdings_user ON holdings(user_id);

-- 2. Migrate existing stock_cost from watchlist to holdings (Quantity 0 as migrated placeholder)
INSERT INTO holdings (user_id, symbol, avg_cost, quantity, weighted_delta)
SELECT user_id, symbol, stock_cost, 0.0, 0.0
FROM watchlist 
WHERE stock_cost > 0;

-- 3. Refactor watchlist (Remove stock_cost column)
CREATE TABLE watchlist_new (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL,
    symbol TEXT NOT NULL,
    use_llm INTEGER DEFAULT 1,
    last_cross_dir TEXT,
    last_cross_price REAL,
    last_cross_time INTEGER,
    UNIQUE(user_id, symbol)
);

INSERT INTO watchlist_new (user_id, symbol, use_llm, last_cross_dir, last_cross_price, last_cross_time)
SELECT user_id, symbol, COALESCE(use_llm, 1), last_cross_dir, last_cross_price, last_cross_time 
FROM watchlist;

DROP TABLE watchlist;
ALTER TABLE watchlist_new RENAME TO watchlist;
"""
