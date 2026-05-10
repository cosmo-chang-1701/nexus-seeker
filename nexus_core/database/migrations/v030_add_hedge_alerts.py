import sqlite3

version = 30
description = "Add hedge_alerts table for Automated Hedging & Alert Pipeline"

sql = """
CREATE TABLE IF NOT EXISTS hedge_alerts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL,
    vix_level REAL NOT NULL,
    vix_stage_move INTEGER DEFAULT 0, -- Stages moved (e.g., +2)
    portfolio_delta REAL NOT NULL,
    portfolio_vega REAL NOT NULL,
    hedge_instrument TEXT NOT NULL, -- e.g., 'SPY'
    hedge_contracts INTEGER NOT NULL,
    instruction_text TEXT NOT NULL,
    narration TEXT, -- LLM generated explanation
    status TEXT DEFAULT 'PENDING', -- PENDING, EXECUTED, CANCELLED
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    executed_at TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_hedge_alerts_user_status ON hedge_alerts(user_id, status);
"""

def migrate_data(conn: sqlite3.Connection):
    pass
