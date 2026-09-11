from typing import List

from database.connection import execute_write_many, get_read_connection


def get_watchlist_tags(user_id: str, symbol: str) -> List[str]:
    """取得特定使用者與標的的標籤清單"""
    conn = get_read_connection()
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
    symbol = symbol.upper()
    # DELETE + executemany 必須是同一個交易，否則覆蓋過程中可能出現「舊標籤已刪、
    # 新標籤未寫入」的空窗。走批次寫入入口，整批共用一個交易、只 commit 一次。
    statements: list[tuple] = [
        (
            "DELETE FROM watchlist_tags WHERE user_id = ? AND symbol = ?",
            (user_id, symbol),
        )
    ]
    if tags:
        statements.append(
            (
                "INSERT INTO watchlist_tags (user_id, symbol, tag_name) VALUES (?, ?, ?)",
                [(user_id, symbol, tag) for tag in tags],
                True,
            )
        )
    try:
        execute_write_many(statements)
        return True
    except Exception:
        return False


def get_user_unique_tags(user_id: str) -> List[str]:
    """取得特定使用者目前所有使用中的不重複標籤"""
    conn = get_read_connection()
    cursor = conn.cursor()
    try:
        cursor.execute(
            "SELECT DISTINCT tag_name FROM watchlist_tags WHERE user_id = ? ORDER BY tag_name ASC",
            (user_id,),
        )
        return [row[0] for row in cursor.fetchall()]
    finally:
        conn.close()
