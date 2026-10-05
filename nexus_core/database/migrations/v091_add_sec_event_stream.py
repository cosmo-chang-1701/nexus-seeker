version = 91
description = (
    "新增 SEC EDGAR 申報游標 (sec_filing_cursor)、事件流 (sec_filing_event)、"
    "內部人交易 (insider_transaction) 與治理審查旗標 (governance_flag)"
)
sql = """
CREATE TABLE IF NOT EXISTS sec_filing_cursor (
    symbol TEXT PRIMARY KEY,
    cik TEXT NOT NULL,
    last_accepted_at TEXT NOT NULL,
    last_accession TEXT NOT NULL,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS sec_filing_event (
    accession TEXT PRIMARY KEY,
    symbol TEXT NOT NULL,
    form TEXT NOT NULL,
    items TEXT,
    accepted_at TEXT NOT NULL,
    session TEXT NOT NULL CHECK(session IN ('BMO', 'RTH', 'AMC', 'OVERNIGHT')),
    primary_doc_url TEXT,
    routes_json TEXT,
    is_backfill INTEGER NOT NULL DEFAULT 0,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_sec_filing_event_symbol_date
ON sec_filing_event(symbol, accepted_at);

CREATE TABLE IF NOT EXISTS insider_transaction (
    accession TEXT NOT NULL,
    line_no INTEGER NOT NULL,
    symbol TEXT NOT NULL,
    owner_name TEXT NOT NULL,
    owner_role TEXT,
    is_c_suite INTEGER NOT NULL DEFAULT 0,
    tx_date TEXT NOT NULL,
    tx_code TEXT NOT NULL,
    shares REAL,
    price REAL,
    acquired_disposed TEXT CHECK(acquired_disposed IN ('A', 'D')),
    shares_after REAL,
    is_10b5_1 INTEGER NOT NULL DEFAULT 0,
    is_backfill INTEGER NOT NULL DEFAULT 0,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (accession, line_no)
);

CREATE INDEX IF NOT EXISTS idx_insider_tx_symbol_date
ON insider_transaction(symbol, tx_date);

CREATE TABLE IF NOT EXISTS governance_flag (
    symbol TEXT NOT NULL,
    source_accession TEXT NOT NULL,
    flag_kind TEXT NOT NULL,
    severity TEXT NOT NULL CHECK(severity IN ('INFO', 'REVIEW', 'HIGH', 'CRITICAL')),
    detail_json TEXT,
    expires_at TEXT NOT NULL,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (symbol, source_accession, flag_kind)
);

CREATE INDEX IF NOT EXISTS idx_governance_flag_active
ON governance_flag(symbol, expires_at);
"""
