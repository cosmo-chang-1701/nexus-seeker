from typing import Any

version = 58
description = "Merge UOA into alpha signals and rebalance defense into option defense"

sql = "SELECT 1;"


def migrate_data(conn: Any):  # type: ignore
    cursor = conn.cursor()
    # 1. Get all user_ids currently having any notification settings
    cursor.execute("SELECT DISTINCT user_id FROM user_notification_settings")
    user_ids = [row[0] for row in cursor.fetchall()]

    # Consolidation mapping: new_key -> list of legacy_keys
    mapping = {
        "radar_alpha_signals": ["hb_uoa", "radar_alpha_signals"],
        "option_defense_alert": ["rollover_rebalance_alert", "option_defense_alert"],
    }

    for uid in user_ids:
        for new_key, legacy_keys in mapping.items():
            placeholders = ",".join("?" for _ in legacy_keys)
            cursor.execute(  # nosemgrep
                f"SELECT enabled FROM user_notification_settings WHERE user_id = ? AND notification_key IN ({placeholders})",
                [uid] + legacy_keys,
            )
            rows = cursor.fetchall()
            if rows:
                enabled = 1 if any(row[0] == 1 for row in rows) else 0
                cursor.execute(
                    """
                    INSERT OR REPLACE INTO user_notification_settings (user_id, notification_key, enabled)
                    VALUES (?, ?, ?)
                    """,
                    (uid, new_key, enabled),
                )

    # 2. Delete legacy keys
    deletion_keys = ["hb_uoa", "rollover_rebalance_alert"]

    if deletion_keys:
        placeholders = ",".join("?" for _ in deletion_keys)
        cursor.execute(  # nosemgrep
            f"DELETE FROM user_notification_settings WHERE notification_key IN ({placeholders})",
            deletion_keys,
        )
    conn.commit()
