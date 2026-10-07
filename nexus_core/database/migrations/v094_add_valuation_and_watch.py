version = 94
description = (
    "新增分析師修正動能分數 (revision_score_log)、公允價值與安全邊際 (fair_value_log) "
    "與基本面次日候選名單 (fundamental_watch_candidate)；"
    "eps_estimate_snapshot 新增財期欄位、earnings_surprise 新增財報發布日欄位"
)
# 注意：
# - fair_value / margin_of_safety / score_30d / breadth_ratio 可為 NULL：無效或無法計算時存 NULL，
#   不以 0.0 哨兵值寫入（下游 /fa 與 calibration 依 NULL 與 method = 'NONE' 排除）。
# - 對 v092 既有資料表只做 ADD COLUMN（v092 已在正式 DB 執行，不得修改 v092 本身）。
sql = """
CREATE TABLE IF NOT EXISTS revision_score_log (
    symbol TEXT NOT NULL,
    trading_date TEXT NOT NULL,
    score_30d REAL,
    breadth_ratio REAL,
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
    fair_value REAL,
    margin_of_safety REAL,
    method TEXT NOT NULL DEFAULT 'NONE'
        CHECK(method IN ('BLENDED', 'DCF_ONLY', 'COMPS_ONLY', 'NONE')),
    spot_price REAL,
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

ALTER TABLE eps_estimate_snapshot ADD COLUMN fiscal_period TEXT;

CREATE INDEX IF NOT EXISTS idx_eps_snapshot_symbol_period
ON eps_estimate_snapshot(symbol, fiscal_period, snapshot_date);

ALTER TABLE earnings_surprise ADD COLUMN announced_on TEXT;
"""
