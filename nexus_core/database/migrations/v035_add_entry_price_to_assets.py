import sqlite3
import json
import logging

logger = logging.getLogger(__name__)

version = 35
description = "Add entry_price column to assets table for faster PnL calculation"

sql = """
-- 1. Add entry_price column to assets table
ALTER TABLE assets ADD COLUMN entry_price REAL;

-- 2. Data migration will be handled in migrate_data function
"""


def migrate_data(conn: sqlite3.Connection):
    cursor = conn.cursor()
    try:
        # Fetch all assets with TRADE or HOLDING context
        cursor.execute(
            "SELECT id, context_type, metadata FROM assets WHERE context_type IN ('TRADE', 'HOLDING')"
        )
        rows = cursor.fetchall()

        for row_id, context_type, metadata_json in rows:
            if not metadata_json:
                continue

            metadata = json.loads(metadata_json)
            entry_price = None

            if context_type == "TRADE":
                entry_price = metadata.get("entry_price")
            elif context_type == "HOLDING":
                entry_price = metadata.get("avg_cost")

            if entry_price is not None:
                cursor.execute(
                    "UPDATE assets SET entry_price = ? WHERE id = ?",
                    (entry_price, row_id),
                )

        conn.commit()
        logger.info("Successfully migrated entry_price data to new column.")
    except Exception as e:
        logger.error(f"Failed to migrate entry_price data: {e}")
        conn.rollback()
