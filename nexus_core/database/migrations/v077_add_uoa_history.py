version = 77
description = (
    "新增 uoa_history：持久化每輪心跳偵測到的機構級異常選擇權活動 (UOA)，"
    "供右側進場條件四的 Regime III-B 時間窗回看使用"
)

# 為什麼需要這張表：
# * UOA 目前**只有最新快照**。`kv_cache` 的 `uoa_{SYMBOL}` 是 ON CONFLICT DO UPDATE
#   的 upsert，每輪心跳覆蓋前一輪；記憶體中的期權鏈快取是 BoundedCache、跨程序
#   重啟即失。`sentiment_history` 每列只能存一個 REAL，結構上放不下 UOA 記錄
#   (strike/expiry/type/action/ratio/notional 六個欄位)。
# * 右側條件四原本要求「評估當下」存在一筆機構 CALL BTO。趨勢的續航段是縮量、
#   陰陽交錯的，機構的跨週期買盤早在啟動日就已佈完——要求此刻還有一筆，等於要求
#   主力每 15 分鐘重新表態一次 (handoff.md §1.3 的「事件式進場」病灶)。
#   Regime III-B 改以 _ENTRY_UOA_LOOKBACK_DAYS 個交易日的回看窗判定，需要歷史。
#
# 設計取捨：
# * 不含 user_id：UOA 是標的層級的市場事實，與使用者無關。
# * 去重鍵 (symbol, observed_bar_ts, expiry, strike, opt_type, action)：同一根 15m
#   K 棒內重複觀測到同一筆合約只留一列，避免 15 分鐘心跳 × 多使用者共用標的造成
#   同一事實被記錄多次。`INSERT OR IGNORE` 搭配此唯一索引即可。
# * 只存條件四判定所需的欄位，不存完整 UOA payload (delta/iv/bid/ask 等)：1GB VPS
#   上每標的每輪最多 5 列 (detect_uoa 只回傳名目價值前 5 大)，保留 10 天即足夠
#   覆蓋 5 個交易日的回看窗加上假日緩衝。
# * `observed_at` 供保留期清理使用；`observed_bar_ts` (美東 15 分鐘向下取整) 供
#   去重，比照 regime_evaluation_log 的既有慣例。
sql = """
CREATE TABLE IF NOT EXISTS uoa_history (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    observed_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    observed_bar_ts TEXT NOT NULL,
    symbol TEXT NOT NULL,
    expiry TEXT NOT NULL,
    strike REAL NOT NULL,
    opt_type TEXT NOT NULL,
    action TEXT NOT NULL,
    ratio REAL NOT NULL DEFAULT 0.0,
    notional_value REAL NOT NULL DEFAULT 0.0
);
CREATE UNIQUE INDEX IF NOT EXISTS uq_uoa_history_dedup
    ON uoa_history (symbol, observed_bar_ts, expiry, strike, opt_type, action);
CREATE INDEX IF NOT EXISTS idx_uoa_history_symbol_observed
    ON uoa_history (symbol, observed_at DESC);
"""
