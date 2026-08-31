from typing import Any

version = 65
description = "Add actual_value column to economic_calendar_events for CPI actual-vs-expected tracking"
sql = ""


def migrate_data(conn: Any) -> None:
    cursor = conn.cursor()
    try:
        cursor.execute(
            "ALTER TABLE economic_calendar_events ADD COLUMN actual_value REAL"
        )
    except Exception as e:
        if (
            "duplicate column name" in str(e).lower()
            or "already exists" in str(e).lower()
        ):
            pass
        else:
            raise e
