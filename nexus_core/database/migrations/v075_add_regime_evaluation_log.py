version = 75
description = (
    "新增 regime_evaluation_log / regime_evaluation_outcome：記錄每次 Regime 分類與"
    "進場鐵律評估當下實際使用的 GEX／價量數值，並由離峰排程回填事後走勢，"
    "作為回測校準 GEX 相關門檻的前向蒐集資料源"
)

# 設計取捨：
# * 不含 user_id：閘門結果只取決於標的與當下市況，與使用者無關；省略後可跨使用者
#   去重 (唯一索引 symbol + evaluator + source + bar_ts)，資料量不隨使用者數放大。
# * 只存「評估當下實際使用的純量」與 ≤ 2KB 的 features_json，**不存完整
#   gex_profile**：1GB VPS 上每日約 260 列，完整 profile 會把年資料量放大數十倍。
# * outcome 表方向中性 (只記錄價格往哪走、先觸及哪條 ATR 帶)，方向由分析端依
#   log.direction 套用，才能做反事實分析 (例如 Regime II 若做空會如何)。
sql = """
CREATE TABLE IF NOT EXISTS regime_evaluation_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    evaluated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    bar_ts TEXT NOT NULL,
    symbol TEXT NOT NULL,
    source TEXT NOT NULL,
    evaluator TEXT NOT NULL,
    regime TEXT,
    direction TEXT,
    sub_mode TEXT,
    decision INTEGER NOT NULL,
    conditions_mask INTEGER,
    conditions_evaluated_mask INTEGER,
    spot REAL,
    gamma_flip REAL,
    call_wall REAL,
    put_wall REAL,
    resistance_wall REAL,
    next_negative_node REAL,
    net_gex REAL,
    session_vwap REAL,
    atr_15m REAL,
    atr_1d REAL,
    rsi_15m REAL,
    volume_ratio REAL,
    ivr REAL,
    vix_spot REAL,
    macro_regime TEXT,
    vts_ratio REAL,
    entry_price REAL,
    stop_price REAL,
    target_price REAL,
    features_json TEXT,
    reason_digest TEXT
);
CREATE UNIQUE INDEX IF NOT EXISTS uq_regime_eval_dedup
    ON regime_evaluation_log(symbol, evaluator, source, bar_ts);
CREATE INDEX IF NOT EXISTS idx_regime_eval_time
    ON regime_evaluation_log(evaluated_at);

CREATE TABLE IF NOT EXISTS regime_evaluation_outcome (
    evaluation_id INTEGER PRIMARY KEY,
    label_status TEXT NOT NULL,
    label_version INTEGER NOT NULL,
    labeled_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    entry_ref_price REAL,
    atr_1d_ref REAL,
    fwd_ret_1h REAL,
    fwd_ret_eod REAL,
    fwd_ret_1d REAL,
    fwd_ret_3d REAL,
    fwd_ret_5d REAL,
    max_up_atr_5d REAL,
    max_down_atr_5d REAL,
    first_touch_k1 INTEGER,
    first_touch_k15 INTEGER,
    first_touch_k2 INTEGER,
    touch_bars_k15 INTEGER,
    plan_outcome INTEGER
);
"""
