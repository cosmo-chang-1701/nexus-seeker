version = 85
description = (
    "新增 user_settings 提領設定：withdrawal_amount（每次提領額，基準月購買力；0 = 未啟用）、"
    "withdrawal_anchor_month（基準月 YYYY-MM，通膨調整的起點）、"
    "withdrawal_months（每年提領月份，逗號分隔）、"
    "withdrawal_target_weights（賣出清單的目標權重 JSON；NULL = 現有持股等權）"
)

# 為什麼是新欄位而非重用 monthly_expense：舊欄位是「每月支出」且只餵給已被取代的
# Theta 跑道；新跑道以「每次提領額 + 月份 + 通膨基準」建模，語意不同。
# monthly_expense 於階段二移除顯示後才淘汰，本遷移不動它。
sql = """
ALTER TABLE user_settings ADD COLUMN withdrawal_amount REAL DEFAULT 0.0;
ALTER TABLE user_settings ADD COLUMN withdrawal_anchor_month TEXT DEFAULT NULL;
ALTER TABLE user_settings ADD COLUMN withdrawal_months TEXT DEFAULT '1,7';
ALTER TABLE user_settings ADD COLUMN withdrawal_target_weights TEXT DEFAULT NULL;
"""
