version = 41
description = "Create archived_assets table for expired portfolio options"
sql = """
CREATE TABLE IF NOT EXISTS archived_assets (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL,
    symbol TEXT NOT NULL,
    context_type TEXT NOT NULL,
    risk_weight REAL DEFAULT 1.0,
    metadata TEXT,
    last_scan_id TEXT,
    created_at TIMESTAMP,
    updated_at TIMESTAMP,
    archived_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_archived_assets_user ON archived_assets(user_id);
"""
