version = 93
description = "新增實體產業鏈與高頻通道交叉驗證日誌 (channel_check_log)"
sql = """
CREATE TABLE IF NOT EXISTS channel_check_log (
    link_key TEXT NOT NULL,
    as_of_period TEXT NOT NULL,
    link_type TEXT NOT NULL CHECK(link_type IN ('CAUSAL', 'NOWCAST')),
    experimental INTEGER NOT NULL DEFAULT 0,
    driver_growth REAL,
    follower_growth REAL,
    divergence_pp REAL,
    nowcast_direction TEXT CHECK(nowcast_direction IN ('NOWCAST_UP', 'NOWCAST_DOWN', 'FLAT', NULL)),
    nowcast_hit INTEGER,
    correlation REAL,
    verdict TEXT NOT NULL CHECK(verdict IN ('CONFIRM', 'DIVERGE', 'INSUFFICIENT')),
    members_json TEXT NOT NULL,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (link_key, as_of_period)
);

CREATE INDEX IF NOT EXISTS idx_channel_check_period
ON channel_check_log(as_of_period, verdict);
"""
