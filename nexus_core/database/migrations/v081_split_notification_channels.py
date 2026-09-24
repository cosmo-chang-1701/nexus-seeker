from typing import Any

version = 81
description = (
    "依對投組下行風險的影響重整通知頻道：新增 10 個子頻道並以母頻道既有設定回填，"
    "拆除 defense_portfolio_risk"
)
sql = ""

# (新頻道, 母頻道)。對照表刻意寫死於此，而不從 database/notification_channels.py
# 匯入：遷移必須是當下的凍結快照，註冊表日後調整 parent_key 不應改變已套用的遷移。
_CHILD_TO_PARENT: tuple[tuple[str, str], ...] = (
    ("defense_event_calendar", "defense_macro_tail_risk"),
    ("defense_hedge_advice", "defense_macro_tail_risk"),
    ("defense_structure_break", "defense_option_rollover"),
    ("defense_gamma_fragility", "defense_portfolio_risk"),
    ("trim_profit_lock", "defense_portfolio_risk"),
    ("entry_pyramid_add", "defense_option_rollover"),
    ("trim_covered_call", "defense_option_rollover"),
    ("vtr_virtual_trades", "defense_option_rollover"),
    ("alpha_short_entry", "alpha_market_signals"),
    ("intel_market_scenario", "heartbeat_watchlist"),
)


def migrate_data(conn: Any) -> None:
    """以母頻道的明確設定回填子頻道，然後移除已拆解的 defense_portfolio_risk。

    若不回填，凡是曾關閉母頻道的使用者都不會有子頻道的資料列，
    `is_notification_enabled()` 便會落到預設值 True，等於把他們靜音的推播自動重新
    打開（與 v070 / v078 的失效模式相同）。僅回填已有明確設定的使用者；從未設定過的
    使用者維持沿用預設值。`INSERT OR IGNORE` 確保重跑不會覆寫使用者事後手動調整過的設定。

    `defense_portfolio_risk` 拆完即無剩餘內容；其舊列若保留，會經別名解析成
    `defense_gamma_fragility` 並與回填列競爭（結果取決於列的順序），使用者之後切換
    新頻道時可能被舊列蓋回。故回填完成後刪除。

    MARGIN_API 模擬保證金警報併入 `defense_margin_call`，刻意不回填：保證金屬帳戶
    生存等級，不應因使用者過去關閉「持倉負 Gamma／DITM／保證金警戒」這個混裝頻道而靜音。
    """
    cursor = conn.cursor()
    for child, parent in _CHILD_TO_PARENT:
        cursor.execute(
            """
            INSERT OR IGNORE INTO user_notification_settings
                (user_id, notification_key, enabled)
            SELECT user_id, ?, enabled
            FROM user_notification_settings
            WHERE notification_key = ?
            """,
            (child, parent),
        )
    cursor.execute(
        "DELETE FROM user_notification_settings "
        "WHERE notification_key = 'defense_portfolio_risk'"
    )
