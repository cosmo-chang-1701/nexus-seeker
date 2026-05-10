import sqlite3

version = 32
description = "Add hedge_logs table for real-time attribution and protection score"

sql = """
CREATE TABLE IF NOT EXISTS hedge_logs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL,
    timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    trigger_event TEXT NOT NULL, -- e.g., 'VIX Spike', 'Delta Deviation'
    benchmark_vix REAL,
    benchmark_ivp REAL,
    pre_hedge_delta REAL,
    pre_hedge_vega REAL,
    instrument TEXT NOT NULL,
    qty INTEGER NOT NULL,
    entry_price REAL,
    exit_price REAL,
    pnl_impact REAL,
    protection_score REAL,
    status TEXT DEFAULT 'OPEN'
);

CREATE INDEX IF NOT EXISTS idx_hedge_logs_user ON hedge_logs(user_id);
"""

def migrate_data(conn: sqlite3.Connection):
    pass
