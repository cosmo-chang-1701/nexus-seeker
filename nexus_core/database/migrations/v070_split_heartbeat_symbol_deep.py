from typing import Any

version = 70
description = (
    "將 heartbeat_watchlist 既有設定回填至新拆分出的 heartbeat_symbol_deep 通道，"
    "避免已靜音使用者被自動重新訂閱 30 分鐘個股深度心跳"
)
sql = ""


def migrate_data(conn: Any) -> None:
    """回填 heartbeat_symbol_deep。

    15 分鐘批次雷達與 30 分鐘個股深度心跳原本共用 `heartbeat_watchlist` 一個
    通知 key，現已拆為兩個獨立通道。若不回填，凡是曾把 `heartbeat_watchlist`
    關掉（或套用 `focus` / `mute_intraday` 預設）的使用者，都不會有
    `heartbeat_symbol_deep` 這一列，`is_notification_enabled()` 便會落到
    `DEFAULT_NOTIFICATION_SETTINGS` 的 True，等於把他們靜音掉的心跳自動重新打開。

    僅回填「已經有明確 heartbeat_watchlist 設定」的使用者；從未設定過的使用者
    維持沿用預設值的行為不變。`INSERT OR IGNORE` 確保重跑此 migration 不會覆寫
    使用者在拆分之後才手動調整過的新設定。
    """
    cursor = conn.cursor()
    cursor.execute(
        """
        INSERT OR IGNORE INTO user_notification_settings
            (user_id, notification_key, enabled)
        SELECT user_id, 'heartbeat_symbol_deep', enabled
        FROM user_notification_settings
        WHERE notification_key = 'heartbeat_watchlist'
        """
    )
