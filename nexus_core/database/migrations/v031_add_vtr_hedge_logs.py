import sqlite3

version = 31
description = "Add vtr_hedge_logs table for VTR attribution and self-evolution"

sql = """
CREATE TABLE IF NOT EXISTS vtr_hedge_logs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL,
    timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    strategy_tag TEXT NOT NULL, -- e.g., 'VEGA_SPIKE', 'POLY_EVENT_HEDGE'
    event_context TEXT, -- JSON: Polymarket events, odds, whale intent
    pre_hedge_greeks TEXT, -- JSON: Delta, Gamma, Vega, Vanna
    theoretical_pnl_delta REAL, -- PnL difference hedged vs unhedged
    protection_score REAL, -- 0-100
    cost_of_hedge REAL,
    loss_avoided REAL,
    status TEXT DEFAULT 'OPEN' -- OPEN, CLOSED
);

CREATE INDEX IF NOT EXISTS idx_vtr_hedge_user ON vtr_hedge_logs(user_id);
"""

def migrate_data(conn: sqlite3.Connection):
    pass
