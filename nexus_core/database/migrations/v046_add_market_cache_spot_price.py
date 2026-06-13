version = 46
description = (
    "Add reference_spot_price column to market_cache table for invalidation checking"
)
sql = """
CREATE TABLE IF NOT EXISTS market_cache (
    symbol TEXT PRIMARY KEY,
    scraped_at TIMESTAMP,
    gex_data TEXT,
    put_wall REAL,
    gamma_flip REAL,
    vix_value REAL,
    status TEXT
);
"""


def migrate_data(conn):
    cursor = conn.cursor()
    # 1. Add reference_spot_price to market_cache
    try:
        cursor.execute("ALTER TABLE market_cache ADD COLUMN reference_spot_price REAL;")
    except Exception as e:
        if (
            "duplicate column name" in str(e).lower()
            or "already exists" in str(e).lower()
        ):
            pass
        else:
            raise e

    # 2. Add consensus_value to economic_calendar_events
    try:
        cursor.execute(
            "ALTER TABLE economic_calendar_events ADD COLUMN consensus_value TEXT;"
        )
    except Exception as e:
        if (
            "duplicate column name" in str(e).lower()
            or "already exists" in str(e).lower()
        ):
            pass
        else:
            raise e
