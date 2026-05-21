import sqlite3

version = 36
description = "Create historical_iv table to store daily implied volatility for symbols"

sql = """
CREATE TABLE IF NOT EXISTS historical_iv (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    symbol TEXT NOT NULL,
    iv REAL NOT NULL,
    date TEXT NOT NULL,
    timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_historical_iv_symbol_date ON historical_iv(symbol, date);
"""


def migrate_data(conn: sqlite3.Connection):
    pass
