version = 52
description = "Change market_cache primary key to composite (symbol, expiry)"
sql = "SELECT 1;"


def migrate_data(conn):
    cursor = conn.cursor()
    # 1. Rename existing table if it exists
    try:
        cursor.execute("ALTER TABLE market_cache RENAME TO market_cache_old;")
    except Exception:
        pass

    # 2. Create the new table with (symbol, expiry) composite primary key
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS market_cache (
            symbol TEXT NOT NULL,
            expiry TEXT NOT NULL,
            max_pain REAL,
            expected_move_lower REAL,
            expected_move_upper REAL,
            reference_spot_price REAL,
            is_stale INTEGER DEFAULT 0,
            calculation_mode TEXT DEFAULT 'OI',
            is_degraded INTEGER DEFAULT 0,
            circuit_breaker_triggered INTEGER DEFAULT 0,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            PRIMARY KEY (symbol, expiry)
        );
    """)

    # 3. If old table exists, migrate data and drop old table
    try:
        cursor.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='market_cache_old';"
        )
        if cursor.fetchone():
            cursor.execute("PRAGMA table_info(market_cache_old);")
            columns = [row[1] for row in cursor.fetchall()]

            common_cols = [
                "symbol",
                "max_pain",
                "expected_move_lower",
                "expected_move_upper",
                "reference_spot_price",
                "is_stale",
                "calculation_mode",
                "is_degraded",
                "circuit_breaker_triggered",
            ]
            valid_cols = [c for c in common_cols if c in columns]

            if "symbol" in valid_cols:
                col_list_str = ", ".join(valid_cols)
                # Since the old table didn't have expiry, default to 'WEEKLY'
                # nosemgrep: python.lang.security.audit.formatted-sql-query.formatted-sql-query
                cursor.execute(f"""
                    INSERT OR REPLACE INTO market_cache (expiry, {col_list_str})
                    SELECT 'WEEKLY', {col_list_str} FROM market_cache_old;
                """)

            cursor.execute("DROP TABLE market_cache_old;")
    except Exception as e:
        print(f"Migration error while migrating old cache: {e}")
