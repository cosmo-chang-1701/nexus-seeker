version = 82
description = (
    "新增 portfolio_nav_daily：每個交易日 16:15 ET 的投組淨值與持股快照，"
    "供已實現回撤 / Sortino 與模擬值並列（market_analysis/downside_monitor.py）"
)

# 為什麼需要這張表：
# * 系統過去沒有任何淨值歷史。`user_settings.capital` 是依當下持倉即時推算的
#   「帳戶規模」，不是時間序列；下行風險監控只能用「現權重 × 1 年歷史報酬」模擬，
#   回答的是「若一直持有目前這組部位會怎樣」，而不是使用者實際經歷的回撤。
#
# 設計取捨：
# * 存持股快照 (positions_json) 而非只存 nav：日報酬以「前一日持股 × 當日價格變化
#   ÷ 前一日 NAV」計算，加碼 / 減碼 / 入金造成的 NAV 變動不會被誤算成報酬。只存
#   nav 的話，任何一筆加碼都會變成一天的「正報酬」，讓 Sortino 與 MDD 失真。
# * (user_id, date) 為主鍵、寫入採 INSERT OR REPLACE：同日重跑（例如重啟後補跑）
#   以最新一次為準。
# * 每位使用者每個交易日一列，252 日約數百列，1GB VPS 上無需保留期清理。
sql = """
CREATE TABLE IF NOT EXISTS portfolio_nav_daily (
    user_id INTEGER NOT NULL,
    date TEXT NOT NULL,
    nav REAL NOT NULL,
    positions_json TEXT NOT NULL,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (user_id, date)
);
"""
