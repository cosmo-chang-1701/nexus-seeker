import sqlite3
import logging

logger = logging.getLogger(__name__)


def upgrade(cursor: sqlite3.Cursor) -> None:
    try:
        cursor.execute(
            """
            CREATE TABLE IF NOT EXISTS fundamental_cache (
                symbol TEXT PRIMARY KEY,
                is_broken INTEGER DEFAULT 0,
                confidence REAL DEFAULT 0.0,
                reasoning TEXT,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
            """
        )
        logger.info("Successfully created fundamental_cache table.")
    except Exception as e:
        logger.error(f"Migration v057 failed: {e}")
        raise


def downgrade(cursor: sqlite3.Cursor) -> None:
    try:
        cursor.execute("DROP TABLE IF EXISTS fundamental_cache")
        logger.info("Successfully dropped fundamental_cache table.")
    except Exception as e:
        logger.error(f"Downgrade v057 failed: {e}")
        raise
