version = 84
description = (
    "新增總經訊號乾跑記錄：macro_series_observation（FRED 觀測的首次所見值與可用日）、"
    "macro_signal_log（每日各指標值與警訊）、macro_regime_log（每日三態判定）；只記錄、不推播"
)

# 為什麼需要這三張表：
# * 歷史回測顯示信用利差 / Sahm / 升息等指標落後，且 LLM 時代後歷史關係可能已改變，
#   因此改以前向資料驗證候選指標（market_analysis/macro_signals.py）。
# * macro_series_observation 以 (series_id, obs_date) 為主鍵、寫入採 INSERT OR IGNORE：
#   FRED 事後修正（例如 ICSA、Sahm）時**保留當時首次看到的值**——這正是前向資料能避免
#   「用修正後數據回測」偏差的原因。上線當天一次載入的歷史則是當時的最新版本。
# * macro_signal_log / macro_regime_log 以交易日（+ 指標）為主鍵、INSERT OR REPLACE：
#   同日重跑（重啟後補跑）以最後一次為準，冪等。
# * 資料量：9 個指標 × 每年約 252 個交易日，數千列；FRED 觀測上線時載入數萬列，
#   1GB VPS 上可忽略，不需保留期清理。
sql = """
CREATE TABLE IF NOT EXISTS macro_series_observation (
    series_id TEXT NOT NULL,
    obs_date TEXT NOT NULL,
    value REAL NOT NULL,
    available_date TEXT NOT NULL,
    first_seen_date TEXT NOT NULL,
    PRIMARY KEY (series_id, obs_date)
);

CREATE TABLE IF NOT EXISTS macro_signal_log (
    trading_date TEXT NOT NULL,
    indicator TEXT NOT NULL,
    value REAL,
    flag INTEGER,
    as_of_date TEXT,
    available_date TEXT,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (trading_date, indicator)
);

CREATE TABLE IF NOT EXISTS macro_regime_log (
    trading_date TEXT PRIMARY KEY,
    raw_state TEXT NOT NULL,
    confirmed_state TEXT,
    flags_json TEXT NOT NULL,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
"""
