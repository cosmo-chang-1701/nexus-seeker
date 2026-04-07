version = 15
description = "建立 daily_market_regime 表格以儲存 VIX 波動率期限結構、30/60 天指標等市場狀態"
sql = """
    CREATE TABLE IF NOT EXISTS daily_market_regime (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        record_date TEXT NOT NULL UNIQUE,
        vts_ratio REAL,
        vix_regime TEXT,
        tail_risk_flag BOOLEAN,
        vix_zscore_30 REAL,
        vix_zscore_60 REAL,
        spy_20ma REAL,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    );
"""
