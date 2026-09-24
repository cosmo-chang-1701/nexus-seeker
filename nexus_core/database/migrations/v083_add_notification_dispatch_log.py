version = 83
description = (
    "新增 notification_dispatch_log / notification_dispatch_outcome：記錄實際送達的"
    "可行動通知（帶標的的進場、減碼、出場訊號），並由離峰排程回填 20 個交易日的"
    "「照做 vs 持有不動」淨值路徑，作為評估各通知頻道對 Sortino / MDD / CVaR 影響的"
    "前向蒐集資料源"
)

# 設計取捨：
# * 只記錄「實際入列」的推播：頻道關閉、去重擋下、乾跑抑制的事件都不在表內——
#   評估的對象是使用者真正收到的訊號，而不是引擎產生過的訊號（後者見
#   regime_evaluation_log）。
# * 唯一索引 (user_id, channel, symbol, scenario, action, trade_date)：同一使用者
#   同日同一事件只留一列。各推播點本身已有每日去重，這裡是第二道防線，避免重啟或
#   去重旗標遺失時同一事件被重複計入樣本。
# * signal_kind / direction / exposure_ratio 由呼叫端明確填入，labeler 只照表計算
#   反事實路徑，不回頭解讀 embed 或頻道語意。
# * outcome 表存逐日報酬序列（JSON，最多 21 個浮點數），讓離線報告可以跨事件合併
#   計算 Sortino 與 CVaR——單一事件 20 天樣本不足以估計 CVaR95。
sql = """
CREATE TABLE IF NOT EXISTS notification_dispatch_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    dispatched_at TEXT NOT NULL,
    trade_date TEXT NOT NULL,
    user_id INTEGER NOT NULL,
    channel TEXT NOT NULL,
    symbol TEXT NOT NULL,
    scenario TEXT NOT NULL DEFAULT '',
    action TEXT NOT NULL DEFAULT '',
    signal_kind TEXT NOT NULL,
    direction TEXT NOT NULL DEFAULT 'LONG',
    exposure_ratio REAL NOT NULL DEFAULT 1.0,
    price REAL
);
CREATE UNIQUE INDEX IF NOT EXISTS uq_notif_dispatch_dedup
    ON notification_dispatch_log(user_id, channel, symbol, scenario, action, trade_date);
CREATE INDEX IF NOT EXISTS idx_notif_dispatch_time
    ON notification_dispatch_log(dispatched_at);

CREATE TABLE IF NOT EXISTS notification_dispatch_outcome (
    dispatch_id INTEGER PRIMARY KEY,
    label_status TEXT NOT NULL,
    label_version INTEGER NOT NULL,
    labeled_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    horizon_days INTEGER,
    entry_ref_price REAL,
    follow_returns_json TEXT,
    hold_returns_json TEXT,
    follow_total_return REAL,
    hold_total_return REAL,
    follow_mdd REAL,
    hold_mdd REAL
);
"""
