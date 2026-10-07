version = 90
description = "新增宏觀流動性體制日誌 (liquidity_regime_log) 與 08:30/10:00 宏觀預期差標準化記錄 (macro_release_surprise)"
sql = """
CREATE TABLE IF NOT EXISTS liquidity_regime_log (
    trading_date TEXT PRIMARY KEY,
    nfci REAL,
    anfci REAL,
    net_liquidity_bn REAL,
    net_liquidity_chg_13w_pct REAL,
    reserves_chg_13w_pct REAL,
    us10y REAL,
    regime TEXT NOT NULL CHECK(regime IN ('EASY', 'NEUTRAL', 'TIGHT', 'UNKNOWN')),
    equity_risk_premium REAL,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS macro_release_surprise (
    event_key TEXT NOT NULL,
    release_time_utc TEXT NOT NULL,
    actual REAL NOT NULL,
    forecast REAL NOT NULL,
    raw_diff REAL NOT NULL,
    z_score REAL,
    growth_sign INTEGER NOT NULL CHECK(growth_sign IN (-1, 1)),
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (event_key, release_time_utc)
);

CREATE INDEX IF NOT EXISTS idx_macro_release_surprise_time
ON macro_release_surprise(release_time_utc);
"""
