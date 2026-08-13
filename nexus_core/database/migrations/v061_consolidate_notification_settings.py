from typing import Any

version = 61
description = "Consolidate notification keys into 10 unified tactical channels"

sql = "SELECT 1;"


def migrate_data(conn: Any) -> None:  # type: ignore
    cursor = conn.cursor()
    # 1. Get all user_ids currently having any notification settings
    cursor.execute("SELECT DISTINCT user_id FROM user_notification_settings")
    user_ids = [row[0] for row in cursor.fetchall()]

    # Consolidation mapping: new_key -> list of legacy_keys
    mapping: dict[str, list[str]] = {
        "briefing_pre_market": ["pre_market_briefing"],
        "briefing_post_market": ["post_market_intelligence"],
        "briefing_weekly_vtr": ["weekly_vtr_report"],
        "heartbeat_watchlist": ["hb_options_structure", "hb_execution_risk"],
        "telemetry_orders": ["order_telemetry_alignment_alert"],
        "defense_portfolio_risk": [
            "margin_and_api_alert",
            "gamma_fragility_alert",
            "profit_lock_alert",
        ],
        "defense_option_rollover": [
            "option_defense_alert",
            "deadlock_recovery_alert",
        ],
        "defense_macro_tail_risk": [
            "volatility_risk_alert",
            "vix_tail_risk_alert",
        ],
        "alpha_market_signals": ["ddp_alert", "volatility_alert"],
        "alpha_polymarket": [
            "polymarket_whale_alert",
            "polymarket_prob_shift_alert",
        ],
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

    # 2. Delete legacy keys (including obsolete radar filter keys)
    legacy_keys_to_delete: list[str] = [
        "pre_market_briefing",
        "post_market_intelligence",
        "weekly_vtr_report",
        "hb_options_structure",
        "hb_execution_risk",
        "order_telemetry_alignment_alert",
        "margin_and_api_alert",
        "gamma_fragility_alert",
        "profit_lock_alert",
        "option_defense_alert",
        "deadlock_recovery_alert",
        "volatility_risk_alert",
        "vix_tail_risk_alert",
        "ddp_alert",
        "volatility_alert",
        "polymarket_whale_alert",
        "polymarket_prob_shift_alert",
        "radar_macro_edge",
        "radar_alpha_signals",
        "radar_risk_defenses",
    ]

    if legacy_keys_to_delete:
        placeholders = ",".join("?" for _ in legacy_keys_to_delete)
        cursor.execute(  # nosemgrep
            f"DELETE FROM user_notification_settings WHERE notification_key IN ({placeholders})",
            legacy_keys_to_delete,
        )
    conn.commit()
