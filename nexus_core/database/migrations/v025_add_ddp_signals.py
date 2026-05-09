version = 25
description = "Add ddp_signals table for Davis Double Play tracking"
sql = """
CREATE TABLE IF NOT EXISTS ddp_signals (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    symbol TEXT NOT NULL,
    current_pe REAL,
    pe_mean_3y REAL,
    eps_growth REAL,
    rev_accel_status TEXT,
    confidence_score REAL,
    confirmed_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    last_notified_at TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_ddp_symbol ON ddp_signals(symbol);
"""
