version = 26
description = "Add pending_notifications table for persistent message queue"
sql = """
CREATE TABLE IF NOT EXISTS pending_notifications (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL,
    content TEXT,
    embed_json TEXT,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    retry_count INTEGER DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_pending_user ON pending_notifications(user_id);
"""
