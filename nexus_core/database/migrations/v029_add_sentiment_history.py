import sqlite3

version = 29
description = "Add sentiment_history table for Skew and PCR tracking"

sql = """
CREATE TABLE IF NOT EXISTS sentiment_history (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    symbol TEXT NOT NULL,
    indicator TEXT NOT NULL, -- 'SKEW', 'PCR'
    value REAL NOT NULL,
    timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_sentiment_symbol_indicator ON sentiment_history(symbol, indicator);
"""


def migrate_data(conn: sqlite3.Connection):
    pass
