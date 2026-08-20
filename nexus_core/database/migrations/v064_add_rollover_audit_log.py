version = 64
description = "新增 rollover_audit_log 資料表，記錄動態轉倉引擎實際推送給使用者的每一則建議，供事後審計與回溯"
sql = """
CREATE TABLE IF NOT EXISTS rollover_audit_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL,
    symbol TEXT NOT NULL,
    scenario TEXT NOT NULL,
    action TEXT NOT NULL,
    sell_ratio REAL NOT NULL DEFAULT 0.0,
    target_core TEXT,
    suggested_price TEXT,
    cash_impact TEXT,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_rollover_audit_log_user_created
    ON rollover_audit_log (user_id, created_at DESC);
"""
