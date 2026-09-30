version = 86
description = (
    "新增 withdrawal_runway_snapshot：每位使用者最新一筆提領跑道快照"
    "（16:15 ET 由 services/withdrawal_runway_service.py 寫入，供面板與盤後報告顯示）"
)

# 每人一列（user_id 為主鍵、INSERT OR REPLACE）：跑道只需要「最新值」，
# 歷史軌跡可由 portfolio_nav_daily 重算，不另存時間序列。
sql = """
CREATE TABLE IF NOT EXISTS withdrawal_runway_snapshot (
    user_id INTEGER PRIMARY KEY,
    as_of TEXT NOT NULL,
    nav REAL NOT NULL,
    nav_date TEXT NOT NULL,
    zero_years REAL NOT NULL,
    gfc_years REAL NOT NULL,
    dotcom_years REAL NOT NULL,
    stress_years REAL NOT NULL,
    capped INTEGER NOT NULL DEFAULT 0,
    next_withdrawal REAL NOT NULL,
    boxx_value REAL NOT NULL DEFAULT 0.0,
    boxx_payments INTEGER NOT NULL DEFAULT 0,
    beta REAL NOT NULL,
    beta_is_fallback INTEGER NOT NULL DEFAULT 0,
    cpi_missing INTEGER NOT NULL DEFAULT 0,
    next_date TEXT,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
"""
