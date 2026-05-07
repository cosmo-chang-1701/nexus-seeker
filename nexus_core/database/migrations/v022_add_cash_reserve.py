version = 22
description = "Add cash_reserve field to user_settings"
sql = "ALTER TABLE user_settings ADD COLUMN cash_reserve REAL DEFAULT 0.0;"
