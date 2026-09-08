from typing import Any

version = 69
description = "新增 market_cache.call_wall 與 market_cache.previous_call_wall 欄位以追蹤做市商阻力牆跨週期遷移"
sql = ""


def migrate_data(conn: Any) -> None:
    cursor = conn.cursor()
    try:
        cursor.execute(
            "ALTER TABLE market_cache ADD COLUMN call_wall REAL DEFAULT NULL"
        )
    except Exception as e:
        if (
            "duplicate column name" in str(e).lower()
            or "already exists" in str(e).lower()
        ):
            pass
        else:
            raise e

    try:
        cursor.execute(
            "ALTER TABLE market_cache ADD COLUMN previous_call_wall REAL DEFAULT NULL"
        )
    except Exception as e:
        if (
            "duplicate column name" in str(e).lower()
            or "already exists" in str(e).lower()
        ):
            pass
        else:
            raise e
