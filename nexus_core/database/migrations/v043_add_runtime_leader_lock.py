version = 43
description = "Add runtime leader lock table for blue-green deploy"
sql = """
CREATE TABLE IF NOT EXISTS runtime_leader_lock (
    name TEXT PRIMARY KEY,
    instance_id TEXT NOT NULL,
    heartbeat_ts INTEGER NOT NULL
);
"""
