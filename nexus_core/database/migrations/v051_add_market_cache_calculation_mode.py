version = 51
description = (
    "Add calculation_mode, is_degraded, and circuit_breaker_triggered to market_cache"
)
sql = "SELECT 1;"  # Placeholder SQL to satisfy database core runner, actual migration done in migrate_data


def migrate_data(conn):
    cursor = conn.cursor()
    columns_to_add = [
        ("calculation_mode", "TEXT DEFAULT 'OI'"),
        ("is_degraded", "INTEGER DEFAULT 0"),
        ("circuit_breaker_triggered", "INTEGER DEFAULT 0"),
    ]
    for col_name, col_type in columns_to_add:
        try:
            cursor.execute(  # nosemgrep
                f"ALTER TABLE market_cache ADD COLUMN {col_name} {col_type}"
            )
        except Exception as e:
            if (
                "duplicate column name" in str(e).lower()
                or "already exists" in str(e).lower()
            ):
                pass
            else:
                raise e
