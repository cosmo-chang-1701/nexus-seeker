version = 92
description = (
    "新增財務預期差綜合評分 (earnings_surprise)、分析師共識快照 (eps_estimate_snapshot) "
    "與管理層前瞻指引語意擷取 (guidance_extraction)"
)
sql = """
CREATE TABLE IF NOT EXISTS earnings_surprise (
    symbol TEXT NOT NULL,
    fiscal_period TEXT NOT NULL,
    actual_eps REAL,
    consensus_eps REAL,
    eps_surprise_pct REAL,
    actual_revenue REAL,
    consensus_revenue REAL,
    revenue_surprise_pct REAL,
    whisper_eps REAL,
    composite_score REAL,
    session TEXT NOT NULL CHECK(session IN ('BMO', 'AMC', 'UNKNOWN')),
    eps_basis TEXT NOT NULL CHECK(eps_basis IN ('VENDOR_ADJUSTED', 'GAAP_EX99')),
    status TEXT NOT NULL CHECK(status IN ('PENDING', 'PROCESSED', 'FAILED')),
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (symbol, fiscal_period)
);

CREATE INDEX IF NOT EXISTS idx_earnings_surprise_symbol
ON earnings_surprise(symbol);

CREATE TABLE IF NOT EXISTS eps_estimate_snapshot (
    symbol TEXT NOT NULL,
    snapshot_date TEXT NOT NULL,
    horizon TEXT NOT NULL CHECK(horizon IN ('0q', '+1q', '0y', '+1y')),
    source TEXT NOT NULL,
    eps_mean REAL NOT NULL,
    eps_high REAL,
    eps_low REAL,
    analyst_count INTEGER,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (symbol, snapshot_date, horizon, source)
);

CREATE INDEX IF NOT EXISTS idx_eps_snapshot_symbol_date
ON eps_estimate_snapshot(symbol, snapshot_date);

CREATE TABLE IF NOT EXISTS guidance_extraction (
    symbol TEXT NOT NULL,
    fiscal_period TEXT NOT NULL,
    source_accession TEXT NOT NULL,
    model_version TEXT NOT NULL,
    confidence_score REAL NOT NULL,
    tone_delta_score REAL NOT NULL,
    data_json TEXT NOT NULL,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (symbol, fiscal_period)
);

CREATE INDEX IF NOT EXISTS idx_guidance_symbol
ON guidance_extraction(symbol);
"""
