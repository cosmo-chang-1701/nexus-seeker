version = 37
description = "Add SQLite-backed macro and earnings calendar caches"
sql = """
CREATE TABLE IF NOT EXISTS economic_calendar_month_cache (
    month_key TEXT PRIMARY KEY,
    checked_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    event_count INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS economic_calendar_events (
    month_key TEXT NOT NULL,
    event TEXT NOT NULL,
    event_time TEXT NOT NULL,
    impact TEXT NOT NULL,
    country TEXT NOT NULL DEFAULT 'US',
    PRIMARY KEY (month_key, event, event_time, country)
);

CREATE INDEX IF NOT EXISTS idx_economic_calendar_events_time
ON economic_calendar_events(event_time);

CREATE TABLE IF NOT EXISTS earnings_calendar_cache (
    symbol TEXT PRIMARY KEY,
    earnings_date TEXT,
    checked_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
"""
