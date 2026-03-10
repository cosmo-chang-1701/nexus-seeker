version = 12
description = "新增 Self-Tuning Hedge 必要欄位與歷史紀錄表"
sql = """
ALTER TABLE user_settings ADD COLUMN dynamic_tau REAL DEFAULT 1.0;

CREATE TABLE IF NOT EXISTS hedge_history (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL,
    date TEXT NOT NULL,
    alpha_pnl REAL DEFAULT 0.0,
    hedge_pnl REAL DEFAULT 0.0,
    effectiveness REAL DEFAULT 0.0,
    tau_applied REAL DEFAULT 1.0
);
"""
