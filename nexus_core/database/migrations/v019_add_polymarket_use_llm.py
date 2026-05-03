version = 19
description = "Add polymarket_use_llm flag to user_settings"
sql = "ALTER TABLE user_settings ADD COLUMN polymarket_use_llm INTEGER DEFAULT 1;"
