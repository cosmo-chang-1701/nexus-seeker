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


# ============================================================================
# 🔔 使用者自訂通知開關 (Notification Toggles)
# ============================================================================

ALL_NOTIFICATION_KEYS = [
    # 定時與掃描背景通知 (Scheduled & Scan)
    "watchlist_heartbeat",
    "pre_market_macro",
    "pre_market_earnings",
    "intraday_execution_guide",
    "intraday_decision_scan",
    "post_market_risk",
    "post_market_ai",
    "post_market_sector_flow",
    "next_day_strategy",
    "weekly_vtr_report",
    "order_telemetry_alignment_alert",
    # 即時風險與事件警報 (Real-time & Events)
    "profit_lock_alert",
    "gamma_fragility_alert",
    "ditm_transition_alert",
    "vtr_settlement_notice",
    "ddp_cheap_vol_alert",
    "proactive_event_alert",
    "global_vol_hedge_alert",
    "polymarket_whale_alert",
]

# 預設通知狀態：大多數維持預設開啟，但允許針對單一 key 預設關閉以避免噪音
DEFAULT_NOTIFICATION_SETTINGS: dict[str, bool] = {
    key: True for key in ALL_NOTIFICATION_KEYS
}
DEFAULT_NOTIFICATION_SETTINGS["order_telemetry_alignment_alert"] = False


def get_user_notification_settings(user_id: int) -> dict[str, bool]:
    """獲取使用者的所有通知開啟狀態（預設由 DEFAULT_NOTIFICATION_SETTINGS 決定）"""
    settings = DEFAULT_NOTIFICATION_SETTINGS.copy()
    try:
        with sqlite3.connect(config.DB_NAME) as conn:
            cursor = conn.cursor()
            cursor.execute(
                """
                SELECT notification_key, enabled
                FROM user_notification_settings
                WHERE user_id = ?
            """,
                (user_id,),
            )
            rows = cursor.fetchall()
            for key, val in rows:
                if key in settings:
                    settings[key] = bool(val)
    except Exception as e:
        logger.error(f"讀取使用者通知設定失敗 (UID: {user_id}): {e}")
    return settings


def set_user_notification_setting(user_id: int, key: str, enabled: bool):
    """新增或更新單一通知設定"""
    if key not in ALL_NOTIFICATION_KEYS:
        logger.warning(f"未知通知 key: {key}")
        return
    try:
        val = 1 if enabled else 0
        with sqlite3.connect(config.DB_NAME) as conn:
            cursor = conn.cursor()
            cursor.execute(
                """
                INSERT INTO user_notification_settings (user_id, notification_key, enabled)
                VALUES (?, ?, ?)
                ON CONFLICT(user_id, notification_key) DO UPDATE SET enabled = excluded.enabled
            """,
                (user_id, key, val),
            )
            conn.commit()
    except Exception as e:
        logger.error(f"儲存使用者通知設定失敗 (UID: {user_id}, Key: {key}): {e}")


def set_all_user_notification_settings(user_id: int, enabled: bool):
    """一鍵開啟或關閉所有通知項目"""
    try:
        val = 1 if enabled else 0
        with sqlite3.connect(config.DB_NAME) as conn:
            cursor = conn.cursor()
            for key in ALL_NOTIFICATION_KEYS:
                cursor.execute(
                    """
                    INSERT INTO user_notification_settings (user_id, notification_key, enabled)
                    VALUES (?, ?, ?)
                    ON CONFLICT(user_id, notification_key) DO UPDATE SET enabled = excluded.enabled
                """,
                    (user_id, key, val),
                )
            conn.commit()
    except Exception as e:
        logger.error(f"一鍵更新所有通知設定失敗 (UID: {user_id}): {e}")


def is_notification_enabled(user_id: int, key: str) -> bool:
    """快速檢查特定通知是否開啟"""
    if key not in ALL_NOTIFICATION_KEYS:
        return True
    try:
        with sqlite3.connect(config.DB_NAME) as conn:
            cursor = conn.cursor()
            cursor.execute(
                """
                SELECT enabled FROM user_notification_settings
                WHERE user_id = ? AND notification_key = ?
            """,
                (user_id, key),
            )
            row = cursor.fetchone()
            if row is not None:
                return bool(row[0])
    except Exception as e:
        logger.error(f"檢查通知狀態失敗 (UID: {user_id}, Key: {key}): {e}")
    return DEFAULT_NOTIFICATION_SETTINGS.get(key, True)
