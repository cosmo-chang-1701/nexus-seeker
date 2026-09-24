"""notification_dispatch_log.py — 已送達可行動通知與其「照做 vs 持有」反事實結果的存取層。

所有寫入一律經 `database/connection.py` 的寫入佇列（單一寫入者不變式，見 AGENTS.md）。
資料表定義見 migrations/v083_add_notification_dispatch_log.py。
"""

import logging
from typing import Any, Mapping, Optional, Sequence

from database.connection import execute_write_many_async, get_read_connection

logger = logging.getLogger(__name__)

LOG_COLUMNS: tuple[str, ...] = (
    "dispatched_at",
    "trade_date",
    "user_id",
    "channel",
    "symbol",
    "scenario",
    "action",
    "signal_kind",
    "direction",
    "exposure_ratio",
    "price",
)

OUTCOME_COLUMNS: tuple[str, ...] = (
    "dispatch_id",
    "label_status",
    "label_version",
    "horizon_days",
    "entry_ref_price",
    "follow_returns_json",
    "hold_returns_json",
    "follow_total_return",
    "hold_total_return",
    "follow_mdd",
    "hold_mdd",
)

_INSERT_LOG_SQL = (
    f"INSERT OR IGNORE INTO notification_dispatch_log ({', '.join(LOG_COLUMNS)}) "  # nosemgrep
    f"VALUES ({', '.join('?' for _ in LOG_COLUMNS)})"
)
_INSERT_OUTCOME_SQL = (
    f"INSERT OR REPLACE INTO notification_dispatch_outcome ({', '.join(OUTCOME_COLUMNS)}) "  # nosemgrep
    f"VALUES ({', '.join('?' for _ in OUTCOME_COLUMNS)})"
)


def _dict_factory(cursor: Any, row: tuple) -> dict[str, Any]:
    return {col[0]: row[i] for i, col in enumerate(cursor.description)}


async def insert_dispatch_records(rows: Sequence[Mapping[str, Any]]) -> int:
    """單一交易批次寫入；重複事件（同使用者／頻道／標的／情境／動作／交易日）忽略。"""
    if not rows:
        return 0
    params = [tuple(row.get(col) for col in LOG_COLUMNS) for row in rows]
    counts = await execute_write_many_async([(_INSERT_LOG_SQL, params, True)])
    return int(counts[0]) if counts else 0


def fetch_pending_dispatches(
    older_than_utc: str,
    limit: int = 300,
    extend_older_than_utc: Optional[str] = None,
    extend_newer_than_utc: Optional[str] = None,
    extended_horizon: int = 60,
) -> list[dict[str, Any]]:
    """讀取待標註的可行動紀錄（非 INFO）：

    1. 尚未標註、送達時間早於 `older_than_utc`；
    2. （有給 `extend_*` 時）已以較短視窗標註、送達時間介於
       `extend_newer_than_utc` 與 `extend_older_than_utc` 之間，可延伸為
       `extended_horizon` 日視窗者。回傳列帶 `label_status` / `horizon_days` 供判斷。
    """
    conn = None
    try:
        conn = get_read_connection()
        conn.row_factory = _dict_factory
        cursor = conn.cursor()
        extend = extend_older_than_utc is not None and extend_newer_than_utc is not None
        cursor.execute(
            """
            SELECT l.*, o.label_status AS label_status, o.horizon_days AS horizon_days
            FROM notification_dispatch_log l
            LEFT JOIN notification_dispatch_outcome o ON o.dispatch_id = l.id
            WHERE l.signal_kind != 'INFO'
              AND (
                (o.dispatch_id IS NULL AND l.dispatched_at <= ?)
                OR (
                  ? = 1
                  AND o.label_status = 'LABELED'
                  AND o.horizon_days < ?
                  AND l.dispatched_at <= ?
                  AND l.dispatched_at >= ?
                )
              )
            ORDER BY l.dispatched_at ASC
            LIMIT ?
            """,
            (
                older_than_utc,
                1 if extend else 0,
                int(extended_horizon),
                extend_older_than_utc or "",
                extend_newer_than_utc or "",
                int(limit),
            ),
        )
        return list(cursor.fetchall())
    except Exception as e:
        logger.error(f"讀取待標註通知紀錄失敗: {e}")
        return []
    finally:
        if conn:
            conn.close()


async def save_dispatch_outcomes(outcomes: Sequence[Mapping[str, Any]]) -> int:
    if not outcomes:
        return 0
    params = [tuple(o.get(col) for col in OUTCOME_COLUMNS) for o in outcomes]
    counts = await execute_write_many_async([(_INSERT_OUTCOME_SQL, params, True)])
    return int(counts[0]) if counts else 0


def load_labeled_dispatches(conn: Any) -> list[dict[str, Any]]:
    """離線報告用：以呼叫端提供的（唯讀）連線讀取已標註的通知與結果。"""
    conn.row_factory = _dict_factory
    cursor = conn.cursor()
    cursor.execute(
        """
        SELECT l.id, l.dispatched_at, l.trade_date, l.channel, l.symbol, l.scenario,
               l.action, l.signal_kind, l.direction, l.exposure_ratio, o.horizon_days,
               o.follow_returns_json, o.hold_returns_json,
               o.follow_total_return, o.hold_total_return,
               o.follow_mdd, o.hold_mdd
        FROM notification_dispatch_log l
        JOIN notification_dispatch_outcome o ON o.dispatch_id = l.id
        WHERE o.label_status = 'LABELED'
        ORDER BY l.dispatched_at ASC
        """
    )
    return list(cursor.fetchall())
