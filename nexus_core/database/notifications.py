from typing import Any
import json
import logging
import threading
import time
from typing import List, Tuple, Optional

from database.connection import (
    execute_write,
    execute_write_many,
    get_read_connection,
)
from database.notification_channels import (
    ALL_NOTIFICATION_KEYS as ALL_NOTIFICATION_KEYS,
    DEFAULT_NOTIFICATION_SETTINGS as DEFAULT_NOTIFICATION_SETTINGS,
    PRESET_PROFILES as PRESET_PROFILES,
)

logger = logging.getLogger(__name__)


def add_pending_notification(
    user_id: int, content: Optional[str] = None, embed_dict: Optional[dict] = None
) -> Any:
    """將待發送通知存入資料庫"""
    try:
        embed_json = json.dumps(embed_dict) if embed_dict else None
        execute_write(
            """
            INSERT INTO pending_notifications (user_id, content, embed_json)
            VALUES (?, ?, ?)
        """,
            (user_id, content, embed_json),
        )
    except Exception as e:
        logger.error(f"儲存待發送通知失敗: {e}")


def get_pending_notifications(
    limit: int = 50,
) -> List[Tuple[int, int, Optional[str], Optional[dict]]]:
    """獲取待發送通知清單"""
    results = []
    conn = None
    try:
        conn = get_read_connection()
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
    try:
        execute_write("DELETE FROM pending_notifications WHERE id = ?", (notif_id,))
    except Exception as e:
        logger.error(f"刪除通知 {notif_id} 失敗: {e}")


def get_pending_count() -> int:
    """獲取剩餘待發送數量"""
    conn = None
    try:
        conn = get_read_connection()
        cursor = conn.cursor()
        cursor.execute("SELECT COUNT(*) FROM pending_notifications")
        return cursor.fetchone()[0]  # type: ignore
    except Exception:
        return 0
    finally:
        if conn:
            conn.close()


# ============================================================================
# 🔔 使用者自訂通知開關
# ============================================================================
# 頻道定義（key、分組、標籤、預設值、預設情境）的單一真實來源是
# database/notification_channels.py；以下名稱為相容既有呼叫者而重新匯出。

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
    # defense_portfolio_risk 於 v081 拆分：負 Gamma → defense_gamma_fragility、
    # DITM 獲利鎖定 → trim_profit_lock、模擬保證金 → defense_margin_call。
    # 殘留的舊列（v081 會刪除）與舊呼叫一律解析到左尾防護的 defense_gamma_fragility。
    "defense_portfolio_risk": "defense_gamma_fragility",
    "margin_and_api_alert": "defense_margin_call",
    "gamma_fragility_alert": "defense_gamma_fragility",
    "profit_lock_alert": "trim_profit_lock",
    "option_defense_alert": "defense_option_rollover",
    "deadlock_recovery_alert": "defense_option_rollover",
    "volatility_risk_alert": "defense_macro_tail_risk",
    "vix_tail_risk_alert": "defense_macro_tail_risk",
    # Alpha & Polymarket & Commodities
    "ddp_alert": "alpha_market_signals",
    "volatility_alert": "alpha_market_signals",
    "polymarket_whale_alert": "alpha_polymarket",
    "polymarket_prob_shift_alert": "alpha_polymarket",
    "wti_oil_alert": "alpha_wti_oil",
    "oil_alert": "alpha_wti_oil",
    # Obsolete Radar filters alias to alpha/defense
    "radar_macro_edge": "defense_macro_tail_risk",
    "radar_alpha_signals": "alpha_market_signals",
    "radar_risk_defenses": "defense_gamma_fragility",
}


# ---------------------------------------------------------------------------
# 每使用者設定快取
# ---------------------------------------------------------------------------
# 推播熱路徑以「使用者 × 標的」為單位查詢開關（價量警報每個 watch、深度心跳每個
# 非綠燈標的、動態轉倉每條指令），每次都開一條同步 SQLite 連線且跑在 event loop 上。
# 同一位使用者的設定在一個排程週期內幾乎不會變，因此快取整份設定 dict：
# - 本程序內的任何寫入（單項 / 全開全關 / 預設情境）都會立即失效該使用者的快取；
# - TTL 只用來涵蓋「其他程序寫入」（藍綠部署期間兩個容器並存）這種罕見情形。
_SETTINGS_CACHE_TTL_SECONDS = 60.0
_settings_cache: dict[int, tuple[float, dict[str, bool]]] = {}
_settings_cache_lock = threading.Lock()


def clear_notification_settings_cache(user_id: Optional[int] = None) -> None:
    """清除設定快取（`user_id` 為 None 時全部清除；測試與寫入路徑使用）。"""
    with _settings_cache_lock:
        if user_id is None:
            _settings_cache.clear()
        else:
            _settings_cache.pop(user_id, None)


def _cache_get(user_id: int) -> Optional[dict[str, bool]]:
    with _settings_cache_lock:
        hit = _settings_cache.get(user_id)
        if hit is None:
            return None
        expires_at, settings = hit
        if time.monotonic() >= expires_at:
            _settings_cache.pop(user_id, None)
            return None
        return settings.copy()


def _cache_put(user_id: int, settings: dict[str, bool]) -> None:
    with _settings_cache_lock:
        _settings_cache[user_id] = (
            time.monotonic() + _SETTINGS_CACHE_TTL_SECONDS,
            settings.copy(),
        )


def _merge_rows(rows: list[tuple[str, int]]) -> dict[str, bool]:
    settings = DEFAULT_NOTIFICATION_SETTINGS.copy()
    for raw_key, val in rows:
        resolved = _resolve_key(raw_key)
        if resolved in settings:
            settings[resolved] = bool(val)
    return settings


_UPSERT_NOTIFICATION_SETTING_SQL = """
    INSERT INTO user_notification_settings (user_id, notification_key, enabled)
    VALUES (?, ?, ?)
    ON CONFLICT(user_id, notification_key) DO UPDATE SET enabled = excluded.enabled
"""


def _resolve_key(key: str) -> str:
    """將舊版 key 別名自動解析為新版 key"""
    return LEGACY_KEY_ALIASES.get(key, key)


def get_user_notification_settings(user_id: int) -> dict[str, bool]:
    """獲取使用者的所有通知開啟狀態（預設由 DEFAULT_NOTIFICATION_SETTINGS 決定）"""
    cached = _cache_get(user_id)
    if cached is not None:
        return cached
    conn = None
    try:
        conn = get_read_connection()
        cursor = conn.cursor()
        cursor.execute(
            """
            SELECT notification_key, enabled
            FROM user_notification_settings
            WHERE user_id = ?
        """,
            (user_id,),
        )
        settings = _merge_rows(cursor.fetchall())
    except Exception as e:
        logger.error(f"讀取使用者通知設定失敗 (UID: {user_id}): {e}")
        # 讀取失敗不寫入快取，下次仍會重試
        return DEFAULT_NOTIFICATION_SETTINGS.copy()
    finally:
        if conn:
            conn.close()
    _cache_put(user_id, settings)
    return settings


def get_notification_settings_many(user_ids: list[int]) -> dict[int, dict[str, bool]]:
    """以單一連線、單一 `IN (...)` 查詢取齊多位使用者的通知設定並填入快取。

    供每輪排程開頭預熱使用，取代迴圈內逐一開連線（比照 `cache.get_kv_cache_many()`）。
    """
    result: dict[int, dict[str, bool]] = {}
    missing: list[int] = []
    for uid in dict.fromkeys(user_ids):
        cached = _cache_get(uid)
        if cached is not None:
            result[uid] = cached
        else:
            missing.append(uid)
    if not missing:
        return result
    conn = None
    try:
        conn = get_read_connection()
        cursor = conn.cursor()
        placeholders = ",".join("?" for _ in missing)
        # nosemgrep: python.lang.security.audit.formatted-sql-query.formatted-sql-query, python.sqlalchemy.security.sqlalchemy-execute-raw-query.sqlalchemy-execute-raw-query
        cursor.execute(
            "SELECT user_id, notification_key, enabled "
            "FROM user_notification_settings "
            f"WHERE user_id IN ({placeholders})",
            missing,
        )
        rows_by_user: dict[int, list[tuple[str, int]]] = {uid: [] for uid in missing}
        for uid, key, val in cursor.fetchall():
            rows_by_user.setdefault(uid, []).append((key, val))
    except Exception as e:
        logger.error(f"批次讀取通知設定失敗: {e}")
        for uid in missing:
            result[uid] = DEFAULT_NOTIFICATION_SETTINGS.copy()
        return result
    finally:
        if conn:
            conn.close()
    for uid in missing:
        settings = _merge_rows(rows_by_user.get(uid, []))
        _cache_put(uid, settings)
        result[uid] = settings
    return result


def set_user_notification_setting(user_id: int, key: str, enabled: bool) -> Any:
    """新增或更新單一通知設定（自動解析別名）"""
    resolved_key = _resolve_key(key)
    if resolved_key not in ALL_NOTIFICATION_KEYS:
        logger.warning(f"未知通知 key: {key} (resolved: {resolved_key})")
        return
    try:
        execute_write(
            _UPSERT_NOTIFICATION_SETTING_SQL,
            (user_id, resolved_key, 1 if enabled else 0),
        )
    except Exception as e:
        logger.error(
            f"儲存使用者通知設定失敗 (UID: {user_id}, Key: {resolved_key}): {e}"
        )
    finally:
        # 寫入完成後才失效：若在寫入前失效，期間的讀取會把舊值重新快取
        clear_notification_settings_cache(user_id)


def set_user_notification_settings_bulk(user_id: int, updates: dict[str, bool]) -> None:
    """以單一交易批次寫入多個頻道開關（自動解析別名、忽略未知 key）。

    `/notif_settings` 的多選送出與「本區全開 / 全關」使用；過去逐 key 各開一次寫入，
    中途被其他寫入插隊會出現半套狀態。
    """
    rows: list[tuple[int, str, int]] = []
    for key, enabled in updates.items():
        resolved = _resolve_key(key)
        if resolved not in ALL_NOTIFICATION_KEYS:
            logger.warning(f"未知通知 key: {key} (resolved: {resolved})")
            continue
        rows.append((user_id, resolved, 1 if enabled else 0))
    if not rows:
        return
    try:
        execute_write_many([(_UPSERT_NOTIFICATION_SETTING_SQL, rows, True)])
    except Exception as e:
        logger.error(f"批次更新通知設定失敗 (UID: {user_id}): {e}")
    finally:
        clear_notification_settings_cache(user_id)


def set_all_user_notification_settings(user_id: int, enabled: bool) -> Any:
    """一鍵開啟或關閉所有通知項目"""
    try:
        val = 1 if enabled else 0
        # 整批共用一個交易（原本是逐 key execute、最後才一次 commit，語意相同），
        # 避免一鍵切換在中途被其他寫入插隊而出現半套狀態。
        execute_write_many(
            [
                (
                    _UPSERT_NOTIFICATION_SETTING_SQL,
                    [(user_id, key, val) for key in ALL_NOTIFICATION_KEYS],
                    True,
                )
            ]
        )
    except Exception as e:
        logger.error(f"一鍵更新所有通知設定失敗 (UID: {user_id}): {e}")
    finally:
        clear_notification_settings_cache(user_id)


def apply_preset_settings(user_id: int, preset_name: str) -> dict[str, bool]:
    """套用特定預設情境模式 (all_on, all_off, focus, mute_intraday)"""
    preset = PRESET_PROFILES.get(preset_name)
    if not preset:
        logger.warning(f"未知預設模式: {preset_name}")
        return get_user_notification_settings(user_id)
    try:
        execute_write_many(
            [
                (
                    _UPSERT_NOTIFICATION_SETTING_SQL,
                    [
                        (user_id, key, 1 if is_on else 0)
                        for key, is_on in preset.items()
                    ],
                    True,
                )
            ]
        )
    except Exception as e:
        logger.error(f"套用預設模式 {preset_name} 失敗 (UID: {user_id}): {e}")
    finally:
        clear_notification_settings_cache(user_id)
    return get_user_notification_settings(user_id)


def is_notification_enabled(user_id: int, key: str) -> bool:
    """快速檢查特定通知是否開啟（自動支援舊 key 別名重定向；讀取走每使用者快取）"""
    resolved_key = _resolve_key(key)
    if resolved_key not in ALL_NOTIFICATION_KEYS:
        return True
    return get_user_notification_settings(user_id).get(
        resolved_key, DEFAULT_NOTIFICATION_SETTINGS.get(resolved_key, True)
    )
