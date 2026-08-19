version = 62
description = "新增 fundamental_scan_state 資料表，作為自動化 SEC 財報掃描的去重游標"
sql = """
CREATE TABLE IF NOT EXISTS fundamental_scan_state (
    symbol TEXT PRIMARY KEY,
    last_accession_number TEXT,
    last_form_type TEXT,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
"""
