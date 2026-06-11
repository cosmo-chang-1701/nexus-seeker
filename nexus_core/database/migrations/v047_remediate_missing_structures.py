version = 47
description = (
    "Remediate missing structures (market_cache and economic_calendar_events columns)"
)
sql = """
CREATE TABLE IF NOT EXISTS market_cache (
    symbol TEXT PRIMARY KEY,
    cached_at TIMESTAMP,
    data TEXT
);
"""


def migrate_data(conn):
    cursor = conn.cursor()

    # 1. Remediate columns in market_cache
    columns_to_add = [
        ("max_pain", "REAL"),
        ("expected_move_lower", "REAL"),
        ("expected_move_upper", "REAL"),
        ("reference_spot_price", "REAL"),
        ("updated_at", "TIMESTAMP DEFAULT CURRENT_TIMESTAMP"),
        ("cached_at", "TIMESTAMP"),
        ("data", "TEXT"),
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

    # 2. Remediate columns in economic_calendar_events
    calendar_cols = [("consensus_value", "TEXT"), ("fedwatch_probability", "REAL")]
    for col_name, col_type in calendar_cols:
        try:
            cursor.execute(  # nosemgrep
                f"ALTER TABLE economic_calendar_events ADD COLUMN {col_name} {col_type}"
            )
        except Exception as e:
            if (
                "duplicate column name" in str(e).lower()
                or "already exists" in str(e).lower()
            ):
                pass
            else:
                raise e
