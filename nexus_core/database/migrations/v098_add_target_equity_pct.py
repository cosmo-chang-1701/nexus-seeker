version = 98
description = (
    "新增 user_settings.target_equity_pct：宣告的股票占比（0–100，其餘為 BOXX＋現金；"
    "-1 = 未啟用配置偏離提醒）"
)

# -1 = 未啟用配置偏離提醒（docs/strategies/09 §7）。預設 -1.0，既有使用者遷移後
# 維持關閉，須在 /settings 主動設定才啟用；0 是合法目標（全數 BOXX），不能當作未啟用。
sql = "ALTER TABLE user_settings ADD COLUMN target_equity_pct REAL DEFAULT -1.0;"
