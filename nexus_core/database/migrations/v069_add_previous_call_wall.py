from typing import Any

version = 69
description = "新增 market_cache.call_wall 與 market_cache.previous_call_wall 欄位以追蹤做市商阻力牆跨週期遷移"
sql = "SELECT 1;"  # Placeholder SQL to satisfy database core runner, actual migration done in migrate_data


def migrate_data(conn: Any) -> None:
    cursor = conn.cursor()
    columns_to_add = [
        ("call_wall", "REAL DEFAULT NULL"),
        ("previous_call_wall", "REAL DEFAULT NULL"),
    ]
    for col_name, col_type in columns_to_add:
        try:
            cursor.execute(f"ALTER TABLE market_cache ADD COLUMN {col_name} {col_type}")
        except Exception as e:
            if (
                "duplicate column name" in str(e).lower()
                or "already exists" in str(e).lower()
            ):
                pass
            else:
                raise e
