from typing import Any

version = 97
description = (
    "新增 wall_anchor（前一交易日收盤牆錨點）與 wall_break_events（GEX 牆突破／跌破事件"
    "與 T+1 結算紀錄），並回填 alpha_wall_break 通知通道"
)
sql = """
CREATE TABLE IF NOT EXISTS wall_anchor (
    symbol TEXT NOT NULL,
    trade_date TEXT NOT NULL,          -- ET 'YYYY-MM-DD'
    call_wall REAL,
    put_wall REAL,
    gex_expiry TEXT,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (symbol, trade_date)
);
CREATE TABLE IF NOT EXISTS wall_break_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    symbol TEXT NOT NULL,
    trade_date TEXT NOT NULL,          -- ET date of the event bar
    direction TEXT NOT NULL,           -- 'BREAKOUT' | 'BREAKDOWN'
    trigger_kind TEXT NOT NULL,        -- 'CLOSE_BEYOND' | 'WICK_REJECT'
    bar_time TEXT NOT NULL,            -- ET 'YYYY-MM-DD HH:MM' bar start
    anchor_trade_date TEXT NOT NULL,
    old_wall REAL NOT NULL,
    close REAL NOT NULL,
    verdict TEXT NOT NULL,             -- 'CANDIDATE' | 'FAKE' | 'UNDETERMINED'
    label TEXT NOT NULL,               -- e.g. 'TRUE_BREAKOUT'
    gates_json TEXT NOT NULL,
    old_wall_oi REAL,
    oi_expiry TEXT,
    gex_expiry TEXT,
    old_wall_gex REAL,
    t1_trade_date TEXT,
    t1_verdict TEXT,                   -- 'CONFIRMED' | 'FAKE' | 'UNDETERMINED'
    t1_label TEXT,
    t1_gates_json TEXT,
    t1_checked_at TIMESTAMP,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    UNIQUE (symbol, trade_date, direction, trigger_kind)
);
CREATE INDEX IF NOT EXISTS idx_wall_break_pending ON wall_break_events(symbol, t1_verdict, trade_date);
CREATE INDEX IF NOT EXISTS idx_wall_break_created ON wall_break_events(created_at);
"""


def migrate_data(conn: Any) -> None:
    """回填 alpha_wall_break 通知通道。

    GEX 牆突破／跌破是 30 分鐘自選雷達管線上的新盤中推播。若不回填，凡是曾套用
    `all_off`、`mute_intraday`、`focus` 或 `bh_defense` 的使用者都不會有
    `alpha_wall_break` 這一列，`is_notification_enabled()` 會落到
    `DEFAULT_NOTIFICATION_SETTINGS` 的 True，等於把他們靜音的盤中推播自動重新打開。

    以 `alpha_price_volume_watch`（同模組、noise=True、INTRADAY）作為「使用者是否
    想要盤中雜訊推播」的代理訊號。僅回填已有明確設定的使用者；從未設定過的使用者
    維持沿用預設值。`INSERT OR IGNORE` 確保重跑不會覆寫使用者事後手動調整的設定。
    """
    cursor = conn.cursor()
    cursor.execute(
        """
        INSERT OR IGNORE INTO user_notification_settings
            (user_id, notification_key, enabled)
        SELECT user_id, 'alpha_wall_break', enabled
        FROM user_notification_settings
        WHERE notification_key = 'alpha_price_volume_watch'
        """
    )
