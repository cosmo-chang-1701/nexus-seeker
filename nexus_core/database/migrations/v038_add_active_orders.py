version = 38
description = "Add active_orders table for pending limit, stop, and trailing orders"
sql = """
CREATE TABLE IF NOT EXISTS active_orders (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL,
    symbol TEXT NOT NULL,
    quantity REAL NOT NULL,
    order_type TEXT NOT NULL,              -- 'MARKET', 'LIMIT', 'STOP', 'STOP_LIMIT', 'TRAILING_STOP_USD', 'TRAILING_STOP_PCT'
    validity TEXT NOT NULL,                -- 'DAY', 'EXT_DAY', 'NIGHT', 'GTC_90'
    limit_price REAL DEFAULT 0.0,
    stop_price REAL DEFAULT 0.0,
    trailing_value REAL DEFAULT 0.0,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_active_orders_user ON active_orders(user_id);
CREATE INDEX IF NOT EXISTS idx_active_orders_symbol ON active_orders(symbol);
"""
