from typing import Any
import sqlite3
import json
import logging
from typing import List, Tuple, Optional
import config

logger = logging.getLogger(__name__)


def add_pending_notification(
    user_id: int, content: Optional[str] = None, embed_dict: Optional[dict] = None
) -> Any:
    """將待發送通知存入資料庫"""
    conn = None
    try:
        embed_json = json.dumps(embed_dict) if embed_dict else None
        conn = sqlite3.connect(config.DB_NAME)
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
    finally:
        if conn:
            conn.close()


def get_pending_notifications(
    limit: int = 50,
) -> List[Tuple[int, int, Optional[str], Optional[dict]]]:
    """獲取待發送通知清單"""
    results = []
    conn = None
    try:
        conn = sqlite3.connect(config.DB_NAME)
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
    finally:
        if conn:
            conn.close()
    return results


def delete_notification(notif_id: int) -> Any:
    """刪除已處理的通知"""
    conn = None
    try:
        conn = sqlite3.connect(config.DB_NAME)
        cursor = conn.cursor()
        cursor.execute("DELETE FROM pending_notifications WHERE id = ?", (notif_id,))
        conn.commit()
    except Exception as e:
        logger.error(f"刪除通知 {notif_id} 失敗: {e}")
    finally:
        if conn:
            conn.close()


def get_pending_count() -> int:
    """獲取剩餘待發送數量"""
    conn = None
    try:
        conn = sqlite3.connect(config.DB_NAME)
        cursor = conn.cursor()
        cursor.execute("SELECT COUNT(*) FROM pending_notifications")
        return cursor.fetchone()[0]  # type: ignore
    except Exception:
        return 0
    finally:
        if conn:
            conn.close()


# ============================================================================
# 🔔 使用者自訂通知開關 (10 大戰術整合頻道)
# ============================================================================

ALL_NOTIFICATION_KEYS: list[str] = [
    # 1. 📋 定時戰報與覆盤 (Scheduled Reports)
    "briefing_pre_market",
    "briefing_post_market",
    "briefing_weekly_vtr",
    # 2. 📡 盤中自選與掛單遙測 (Intraday Telemetry)
    "heartbeat_watchlist",
    "telemetry_orders",
    # 3. 🛡️ 持倉風控與防禦 (Portfolio & Risk Defense)
    "defense_portfolio_risk",
    "defense_option_rollover",
    "defense_macro_tail_risk",
    # 4. 🎯 Alpha 策略與情報 (Alpha & Intelligence)
    "alpha_market_signals",
    "alpha_polymarket",
]

# 舊版 Key 映射字典，確保向下相容
LEGACY_KEY_ALIASES: dict[str, str] = {
    # Briefings
    "pre_market_briefing": "briefing_pre_market",
    "post_market_intelligence": "briefing_post_market",
    "weekly_vtr_report": "briefing_weekly_vtr",
    # Intraday Heartbeat & Telemetry
    "hb_options_structure": "heartbeat_watchlist",
    "hb_execution_risk": "heartbeat_watchlist",
    "order_telemetry_alignment_alert": "telemetry_orders",
    # Portfolio & Risk Defense
    "margin_and_api_alert": "defense_portfolio_risk",
    "gamma_fragility_alert": "defense_portfolio_risk",
    "profit_lock_alert": "defense_portfolio_risk",
    "option_defense_alert": "defense_option_rollover",
    "deadlock_recovery_alert": "defense_option_rollover",
    "volatility_risk_alert": "defense_macro_tail_risk",
    "vix_tail_risk_alert": "defense_macro_tail_risk",
    # Alpha & Polymarket
    "ddp_alert": "alpha_market_signals",
    "volatility_alert": "alpha_market_signals",
    "polymarket_whale_alert": "alpha_polymarket",
    "polymarket_prob_shift_alert": "alpha_polymarket",
    # Obsolete Radar filters alias to alpha/defense
    "radar_macro_edge": "defense_macro_tail_risk",
    "radar_alpha_signals": "alpha_market_signals",
    "radar_risk_defenses": "defense_portfolio_risk",
}

# 預設通知狀態：大多數維持預設開啟
DEFAULT_NOTIFICATION_SETTINGS: dict[str, bool] = {
    key: True for key in ALL_NOTIFICATION_KEYS
}

# 戰術預設情境設定檔 (Presets)
PRESET_PROFILES: dict[str, dict[str, bool]] = {
    "all_on": {key: True for key in ALL_NOTIFICATION_KEYS},
    "all_off": {key: False for key in ALL_NOTIFICATION_KEYS},
    "focus": {
        "briefing_pre_market": True,
        "briefing_post_market": True,
        "briefing_weekly_vtr": True,
        "heartbeat_watchlist": False,
        "telemetry_orders": True,
        "defense_portfolio_risk": True,
        "defense_option_rollover": True,
        "defense_macro_tail_risk": True,
        "alpha_market_signals": False,
        "alpha_polymarket": False,
    },
    "mute_intraday": {
        "briefing_pre_market": True,
        "briefing_post_market": True,
        "briefing_weekly_vtr": True,
        "heartbeat_watchlist": False,
        "telemetry_orders": False,
        "defense_portfolio_risk": True,
        "defense_option_rollover": False,
        "defense_macro_tail_risk": True,
        "alpha_market_signals": False,
        "alpha_polymarket": False,
    },
}


def _resolve_key(key: str) -> str:
    """將舊版 key 別名自動解析為新版 key"""
    return LEGACY_KEY_ALIASES.get(key, key)


def get_user_notification_settings(user_id: int) -> dict[str, bool]:
    """獲取使用者的所有通知開啟狀態（預設由 DEFAULT_NOTIFICATION_SETTINGS 決定）"""
    settings = DEFAULT_NOTIFICATION_SETTINGS.copy()
    conn = None
    try:
        conn = sqlite3.connect(config.DB_NAME)
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
        for raw_key, val in rows:
            resolved = _resolve_key(raw_key)
            if resolved in settings:
                settings[resolved] = bool(val)
    except Exception as e:
        logger.error(f"讀取使用者通知設定失敗 (UID: {user_id}): {e}")
    finally:
        if conn:
            conn.close()
    return settings


def set_user_notification_setting(user_id: int, key: str, enabled: bool) -> Any:
    """新增或更新單一通知設定（自動解析別名）"""
    resolved_key = _resolve_key(key)
    if resolved_key not in ALL_NOTIFICATION_KEYS:
        logger.warning(f"未知通知 key: {key} (resolved: {resolved_key})")
        return
    conn = None
    try:
        val = 1 if enabled else 0
        conn = sqlite3.connect(config.DB_NAME)
        cursor = conn.cursor()
        cursor.execute(
            """
            INSERT INTO user_notification_settings (user_id, notification_key, enabled)
            VALUES (?, ?, ?)
            ON CONFLICT(user_id, notification_key) DO UPDATE SET enabled = excluded.enabled
        """,
            (user_id, resolved_key, val),
        )
        conn.commit()
    except Exception as e:
        logger.error(
            f"儲存使用者通知設定失敗 (UID: {user_id}, Key: {resolved_key}): {e}"
        )
    finally:
        if conn:
            conn.close()


def set_all_user_notification_settings(user_id: int, enabled: bool) -> Any:
    """一鍵開啟或關閉所有通知項目"""
    conn = None
    try:
        val = 1 if enabled else 0
        conn = sqlite3.connect(config.DB_NAME)
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
    finally:
        if conn:
            conn.close()


def apply_preset_settings(user_id: int, preset_name: str) -> dict[str, bool]:
    """套用特定預設情境模式 (all_on, all_off, focus, mute_intraday)"""
    preset = PRESET_PROFILES.get(preset_name)
    if not preset:
        logger.warning(f"未知預設模式: {preset_name}")
        return get_user_notification_settings(user_id)
    conn = None
    try:
        conn = sqlite3.connect(config.DB_NAME)
        cursor = conn.cursor()
        for key, is_on in preset.items():
            val = 1 if is_on else 0
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
        logger.error(f"套用預設模式 {preset_name} 失敗 (UID: {user_id}): {e}")
    finally:
        if conn:
            conn.close()
    return get_user_notification_settings(user_id)


def is_notification_enabled(user_id: int, key: str) -> bool:
    """快速檢查特定通知是否開啟（自動支援舊 key 別名重定向）"""
    resolved_key = _resolve_key(key)
    if resolved_key not in ALL_NOTIFICATION_KEYS:
        return True
    conn = None
    try:
        conn = sqlite3.connect(config.DB_NAME)
        cursor = conn.cursor()
        cursor.execute(
            """
            SELECT enabled FROM user_notification_settings
            WHERE user_id = ? AND notification_key = ?
        """,
            (user_id, resolved_key),
        )
        row = cursor.fetchone()
        if row is not None:
            return bool(row[0])
    except Exception as e:
        logger.error(f"檢查通知狀態失敗 (UID: {user_id}, Key: {resolved_key}): {e}")
    finally:
        if conn:
            conn.close()
    return DEFAULT_NOTIFICATION_SETTINGS.get(resolved_key, True)
