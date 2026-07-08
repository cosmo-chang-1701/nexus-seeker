import sqlite3


def run(conn: sqlite3.Connection):
    cursor = conn.cursor()

    cursor.execute("PRAGMA table_info(user_settings)")
    columns = [col[1] for col in cursor.fetchall()]

    if "can_trade_spreads" not in columns:
        cursor.execute(
            "ALTER TABLE user_settings ADD COLUMN can_trade_spreads BOOLEAN DEFAULT FALSE"
        )

    if "cash_reserve_protection" not in columns:
        cursor.execute(
            "ALTER TABLE user_settings ADD COLUMN cash_reserve_protection BOOLEAN DEFAULT TRUE"
        )

    conn.commit()
