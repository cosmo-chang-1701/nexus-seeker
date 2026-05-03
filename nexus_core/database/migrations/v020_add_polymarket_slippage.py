version = 20
description = "Add polymarket_slippage to user_settings"
sql = "ALTER TABLE user_settings ADD COLUMN polymarket_slippage REAL DEFAULT 2.0;"
