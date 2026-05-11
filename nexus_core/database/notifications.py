import sqlite3
import json
import logging
from typing import List, Tuple, Optional
import config

logger = logging.getLogger(__name__)


def add_pending_notification(
    user_id: int, content: Optional[str] = None, embed_dict: Optional[dict] = None
):
    """將待發送通知存入資料庫"""
    try:
        embed_json = json.dumps(embed_dict) if embed_dict else None
        with sqlite3.connect(config.DB_NAME) as conn:
            cursor = conn.cursor()
            cursor.execute(
                """
                INSERT INTO pending_notifications (user_id, content, embed_json)
                VALUES (?, ?, ?)
            """,
                (user_id, content, embed_json),
            )
            conn.commit()
    except Exception as e:
        logger.error(f"儲存待發送通知失敗: {e}")


def get_pending_notifications(
    limit: int = 50,
) -> List[Tuple[int, int, Optional[str], Optional[dict]]]:
    """獲取待發送通知清單"""
    results = []
    try:
        with sqlite3.connect(config.DB_NAME) as conn:
            cursor = conn.cursor()
            cursor.execute(
                """
                SELECT id, user_id, content, embed_json
                FROM pending_notifications
                ORDER BY created_at ASC
                LIMIT ?
            """,
                (limit,),
            )
            rows = cursor.fetchall()
            for row in rows:
                notif_id, uid, content, e_json = row
                embed_dict = json.loads(e_json) if e_json else None
                results.append((notif_id, uid, content, embed_dict))
    except Exception as e:
        logger.error(f"讀取待發送通知失敗: {e}")
    return results


def delete_notification(notif_id: int):
    """刪除已處理的通知"""
    try:
        with sqlite3.connect(config.DB_NAME) as conn:
            cursor = conn.cursor()
            cursor.execute(
                "DELETE FROM pending_notifications WHERE id = ?", (notif_id,)
            )
            conn.commit()
    except Exception as e:
        logger.error(f"刪除通知 {notif_id} 失敗: {e}")


def get_pending_count() -> int:
    """獲取剩餘待發送數量"""
    try:
        with sqlite3.connect(config.DB_NAME) as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT COUNT(*) FROM pending_notifications")
            return cursor.fetchone()[0]
    except Exception:
        return 0
