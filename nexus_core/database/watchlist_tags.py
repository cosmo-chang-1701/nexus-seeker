import sqlite3
from typing import List
import config


def get_watchlist_tags(user_id: str, symbol: str) -> List[str]:
    """取得特定使用者與標的的標籤清單"""
    conn = sqlite3.connect(config.DB_NAME)
    cursor = conn.cursor()
    try:
        cursor.execute(
            "SELECT tag_name FROM watchlist_tags WHERE user_id = ? AND symbol = ? ORDER BY tag_name ASC",
            (user_id, symbol.upper()),
        )
        return [row[0] for row in cursor.fetchall()]
    finally:
        conn.close()


def set_watchlist_tags(user_id: str, symbol: str, tags: List[str]) -> bool:
    """設定特定使用者與標的的標籤清單 (完全覆蓋)"""
    conn = sqlite3.connect(config.DB_NAME)
    cursor = conn.cursor()
    symbol = symbol.upper()
    try:
        # First delete existing tags
        cursor.execute(
            "DELETE FROM watchlist_tags WHERE user_id = ? AND symbol = ?",
            (user_id, symbol),
        )

        # Insert new tags
        if tags:
            cursor.executemany(
                "INSERT INTO watchlist_tags (user_id, symbol, tag_name) VALUES (?, ?, ?)",
                [(user_id, symbol, tag) for tag in tags],
            )

        conn.commit()
        return True
    except Exception:
        conn.rollback()
        return False
    finally:
        conn.close()


def get_user_unique_tags(user_id: str) -> List[str]:
    """取得特定使用者目前所有使用中的不重複標籤"""
    conn = sqlite3.connect(config.DB_NAME)
    cursor = conn.cursor()
    try:
        cursor.execute(
            "SELECT DISTINCT tag_name FROM watchlist_tags WHERE user_id = ? ORDER BY tag_name ASC",
            (user_id,),
        )
        return [row[0] for row in cursor.fetchall()]
    finally:
        conn.close()
