version = 94
description = (
    "新增分析師修正動能分數 (revision_score_log)、公允價值與安全邊際 (fair_value_log) "
    "與基本面次日候選名單 (fundamental_watch_candidate)"
)
sql = """
CREATE TABLE IF NOT EXISTS revision_score_log (
    symbol TEXT NOT NULL,
    trading_date TEXT NOT NULL,
    score_30d REAL NOT NULL,
    breadth_ratio REAL NOT NULL,
    is_pead_aligned INTEGER NOT NULL DEFAULT 0,
    detail_json TEXT NOT NULL,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (symbol, trading_date)
);

CREATE INDEX IF NOT EXISTS idx_revision_score_date
ON revision_score_log(trading_date);

CREATE TABLE IF NOT EXISTS fair_value_log (
    symbol TEXT NOT NULL,
    trading_date TEXT NOT NULL,
    dcf_value REAL,
    comps_value REAL,
    fair_value REAL NOT NULL,
    margin_of_safety REAL NOT NULL,
    discount_rate REAL NOT NULL,
    equity_risk_premium REAL NOT NULL,
    flags_json TEXT NOT NULL,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (symbol, trading_date)
);

CREATE INDEX IF NOT EXISTS idx_fair_value_date
ON fair_value_log(trading_date);

CREATE TABLE IF NOT EXISTS fundamental_watch_candidate (
    trading_date TEXT NOT NULL,
    symbol TEXT NOT NULL,
    rank INTEGER NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('CANDIDATE', 'WATCH', 'EXCLUDED')),
    reasons_json TEXT NOT NULL,
    excluded_reason TEXT,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (trading_date, symbol)
);

CREATE INDEX IF NOT EXISTS idx_fundamental_watch_date_rank
ON fundamental_watch_candidate(trading_date, rank);

CREATE INDEX IF NOT EXISTS idx_fundamental_watch_symbol
ON fundamental_watch_candidate(symbol);
"""
