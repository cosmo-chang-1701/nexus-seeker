import sqlite3
import json
import logging
from config import DB_NAME

version = 28
description = "Unified Asset Lifecycle: Consolidate watchlist, portfolio, and holdings into Assets table"

sql = """
-- 1. Create unified Assets table
CREATE TABLE IF NOT EXISTS assets (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL,
    symbol TEXT NOT NULL,
    context_type TEXT NOT NULL CHECK (context_type IN ('WATCH', 'TRADE', 'HOLDING')),
    risk_weight REAL DEFAULT 1.0,
    metadata TEXT, -- JSON storage
    last_scan_id TEXT,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_assets_user_context ON assets(user_id, context_type);
CREATE INDEX IF NOT EXISTS idx_assets_symbol ON assets(symbol);

-- 2. Data Migration Logic (to be executed via script)
"""

def migrate_data(conn: sqlite3.Connection):
    cursor = conn.cursor()

    # Helper to check if table exists
    def table_exists(name):
        cursor.execute("SELECT name FROM sqlite_master WHERE type='table' AND name=?", (name,))
        return cursor.fetchone() is not None

    try:
        # 2.1 Migrate WATCHLIST
        if table_exists("watchlist"):
            cursor.execute("SELECT user_id, symbol, use_llm FROM watchlist")
            for uid, sym, use_llm in cursor.fetchall():
                metadata = json.dumps({"use_llm": bool(use_llm)})
                cursor.execute(
                    "INSERT INTO assets (user_id, symbol, context_type, metadata) VALUES (?, ?, 'WATCH', ?)",
                    (uid, sym, metadata)
                )

        # 2.2 Migrate PORTFOLIO (Trades)
        if table_exists("portfolio"):
            cursor.execute("SELECT user_id, symbol, opt_type, strike, expiry, entry_price, quantity, weighted_delta, theta, gamma, trade_category FROM portfolio")
            for uid, sym, o_type, strike, exp, price, qty, w_delta, theta, gamma, cat in cursor.fetchall():
                metadata = json.dumps({
                    "opt_type": o_type,
                    "strike": strike,
                    "expiry": exp,
                    "entry_price": price,
                    "quantity": qty,
                    "weighted_delta": w_delta,
                    "theta": theta,
                    "gamma": gamma,
                    "category": cat or "SPEC"
                })
                cursor.execute(
                    "INSERT INTO assets (user_id, symbol, context_type, metadata, risk_weight) VALUES (?, ?, 'TRADE', ?, 1.0)",
                    (uid, sym, metadata)
                )

        # 2.3 Migrate HOLDINGS
        if table_exists("holdings"):
            cursor.execute("SELECT user_id, symbol, quantity, avg_cost, weighted_delta FROM holdings")
            for uid, sym, qty, cost, w_delta in cursor.fetchall():
                metadata = json.dumps({
                    "quantity": qty,
                    "avg_cost": cost,
                    "weighted_delta": w_delta
                })
                cursor.execute(
                    "INSERT INTO assets (user_id, symbol, context_type, metadata, risk_weight) VALUES (?, ?, 'HOLDING', ?, 1.0)",
                    (uid, sym, metadata)
                )
        conn.commit()
    except Exception as e:
        logger.warning(f"Unified Asset Data Migration partially failed: {e}")
        # We don't rollback here because the Assets table creation was already successful in executescript

# Note: The automated migration engine in database/core.py might need update to handle migrate_data function
# or I just put standard SQL here. Since SQLite doesn't handle JSON easily in SQL scripts for complex migration,
# I will implement the logic within the database/core.py or keep it simple if possible.
