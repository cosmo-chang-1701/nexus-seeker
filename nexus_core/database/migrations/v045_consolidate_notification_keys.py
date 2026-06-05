version = 45
description = "Consolidate 13 legacy notification keys into 6 core notification keys"

sql = "SELECT 1;"


def migrate_data(conn):
    cursor = conn.cursor()
    # 1. Get all user_ids currently having any notification settings
    cursor.execute("SELECT DISTINCT user_id FROM user_notification_settings")
    user_ids = [row[0] for row in cursor.fetchall()]

    # Consolidation mapping: new_key -> list of legacy_keys
    mapping = {
        "pre_market_briefing": ["pre_market_macro", "pre_market_earnings"],
        "intraday_decision_scan": [
            "intraday_execution_guide",
            "intraday_decision_scan",
        ],
        "post_market_intelligence": [
            "post_market_risk",
            "post_market_ai",
            "post_market_sector_flow",
            "next_day_strategy",
        ],
        "watchlist_heartbeat_alignment": [
            "watchlist_heartbeat",
            "order_telemetry_alignment_alert",
        ],
        "option_defense_alert": ["ditm_transition_alert", "vtr_settlement_notice"],
        "volatility_risk_alert": ["proactive_event_alert", "global_vol_hedge_alert"],
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

    # 2. Delete legacy keys (except intraday_decision_scan which is kept as a new key)
    all_legacy_keys = []
    for keys in mapping.values():
        all_legacy_keys.extend(keys)

    deletion_keys = [k for k in all_legacy_keys if k != "intraday_decision_scan"]

    if deletion_keys:
        placeholders = ",".join("?" for _ in deletion_keys)
        cursor.execute(  # nosemgrep
            f"DELETE FROM user_notification_settings WHERE notification_key IN ({placeholders})",
            deletion_keys,
        )
    conn.commit()
