import sqlite3
import logging

version = 33
description = "Unify user_settings column names: portfolio_value -> capital, risk_limit_pct -> risk_limit"

sql = """
-- 1. Create temporary table with new names
CREATE TABLE user_settings_new (
    user_id INTEGER PRIMARY KEY,
    capital REAL NOT NULL DEFAULT 100000.0,
    risk_limit REAL DEFAULT 15.0,
    last_rehedge_alert_time INTEGER DEFAULT 0,
    dynamic_tau REAL DEFAULT 1.0,
    enable_option_alerts BOOLEAN DEFAULT 1,
    enable_vtr BOOLEAN DEFAULT 1,
    enable_psq_watchlist BOOLEAN DEFAULT 0,
    enable_analyst_agent BOOLEAN DEFAULT 0,
    polymarket_threshold REAL DEFAULT 10000.0,
    polymarket_use_llm INTEGER DEFAULT 1,
    polymarket_slippage REAL DEFAULT 2.0,
    is_professional_mode BOOLEAN DEFAULT 1,
    monthly_expense REAL DEFAULT 0.0,
    tax_reserve_rate REAL DEFAULT 0.20,
    cash_reserve REAL DEFAULT 0.0
);

-- 2. Copy data from old table to new table
INSERT INTO user_settings_new (
    user_id, capital, risk_limit, last_rehedge_alert_time, dynamic_tau,
    enable_option_alerts, enable_vtr, enable_psq_watchlist, enable_analyst_agent,
    polymarket_threshold, polymarket_use_llm, polymarket_slippage,
    is_professional_mode, monthly_expense, tax_reserve_rate, cash_reserve
)
SELECT
    user_id, portfolio_value, risk_limit_pct, last_rehedge_alert_time, dynamic_tau,
    enable_option_alerts, enable_vtr, enable_psq_watchlist, enable_analyst_agent,
    polymarket_threshold, polymarket_use_llm, polymarket_slippage,
    is_professional_mode, monthly_expense, tax_reserve_rate, cash_reserve
FROM user_settings;

-- 3. Drop old table and rename new table
DROP TABLE user_settings;
ALTER TABLE user_settings_new RENAME TO user_settings;
"""
