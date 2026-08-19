version = 63
description = (
    "新增 price_volume_watches 資料表，作為個股 15 分鐘價量突破警報的每用戶監測設定"
)
sql = """
CREATE TABLE IF NOT EXISTS price_volume_watches (
    user_id INTEGER NOT NULL,
    symbol TEXT NOT NULL,
    target_price REAL NOT NULL,
    direction TEXT NOT NULL DEFAULT 'above',
    volume_multiplier REAL NOT NULL DEFAULT 1.5,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (user_id, symbol)
);
"""
