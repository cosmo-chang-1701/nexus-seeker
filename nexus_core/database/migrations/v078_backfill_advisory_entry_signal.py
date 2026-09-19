from typing import Any

version = 78
description = (
    "將 heartbeat_symbol_deep 既有設定回填至新增的 advisory_entry_signal 通道，"
    "避免已靜音盤中推播的使用者在進場顧問上線後被自動訂閱"
)
sql = ""


def migrate_data(conn: Any) -> None:
    """回填 advisory_entry_signal。

    自選標的進場顧問是 30 分鐘個股深度心跳管線上的新推播路徑。若不回填，凡是曾
    套用 `mute_intraday` 預設或關閉 `heartbeat_symbol_deep` 的使用者，都不會有
    `advisory_entry_signal` 這一列，`is_notification_enabled()` 便會落到
    `DEFAULT_NOTIFICATION_SETTINGS` 的 True，等於把他們靜音的盤中推播自動重新打開
    （與 v070 拆分 `heartbeat_symbol_deep` 時的失效模式相同）。

    以 `heartbeat_symbol_deep` 作為「使用者是否想要盤中個股推播」的代理訊號。
    僅回填已有明確設定的使用者；從未設定過的使用者維持沿用預設值。
    `INSERT OR IGNORE` 確保重跑不會覆寫使用者事後手動調整過的設定。

    `advisory_core_levels` 刻意不回填：它屬持倉防禦等級的資訊，預設開啟。
    """
    cursor = conn.cursor()
    cursor.execute(
        """
        INSERT OR IGNORE INTO user_notification_settings
            (user_id, notification_key, enabled)
        SELECT user_id, 'advisory_entry_signal', enabled
        FROM user_notification_settings
        WHERE notification_key = 'heartbeat_symbol_deep'
        """
    )
