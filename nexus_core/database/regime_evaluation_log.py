"""regime_evaluation_log.py — Regime／進場鐵律評估紀錄與事後結果標註的存取層。

所有寫入一律經 `database/connection.py` 的寫入佇列 (單一寫入者不變式，見
AGENTS.md)。資料表定義見 migrations/v075_add_regime_evaluation_log.py。
"""

import logging
from typing import Any, Mapping, Optional, Sequence

from database.connection import execute_write_many_async, get_read_connection

logger = logging.getLogger(__name__)

# 與 v075 資料表欄位一一對應 (不含 id / evaluated_at，由 DB 預設)。
LOG_COLUMNS: tuple[str, ...] = (
    "bar_ts",
    "symbol",
    "source",
    "evaluator",
    "regime",
    "direction",
    "sub_mode",
    "decision",
    "conditions_mask",
    "conditions_evaluated_mask",
    "spot",
    "gamma_flip",
    "call_wall",
    "put_wall",
    "resistance_wall",
    "next_negative_node",
    "net_gex",
    "session_vwap",
    "atr_15m",
    "atr_1d",
    "rsi_15m",
    "volume_ratio",
    "ivr",
    "vix_spot",
    "macro_regime",
    "vts_ratio",
    "entry_price",
    "stop_price",
    "target_price",
    "features_json",
    "reason_digest",
)

OUTCOME_COLUMNS: tuple[str, ...] = (
    "evaluation_id",
    "label_status",
    "label_version",
    "entry_ref_price",
    "atr_1d_ref",
    "fwd_ret_1h",
    "fwd_ret_eod",
    "fwd_ret_1d",
    "fwd_ret_3d",
    "fwd_ret_5d",
    "max_up_atr_5d",
    "max_down_atr_5d",
    "first_touch_k1",
    "first_touch_k15",
    "first_touch_k2",
    "touch_bars_k15",
    "plan_outcome",
)

_INSERT_LOG_SQL = (
    f"INSERT OR IGNORE INTO regime_evaluation_log ({', '.join(LOG_COLUMNS)}) "  # nosemgrep
    f"VALUES ({', '.join('?' for _ in LOG_COLUMNS)})"
)
_INSERT_OUTCOME_SQL = (
    f"INSERT OR REPLACE INTO regime_evaluation_outcome ({', '.join(OUTCOME_COLUMNS)}) "  # nosemgrep
    f"VALUES ({', '.join('?' for _ in OUTCOME_COLUMNS)})"
)


async def insert_regime_evaluations(rows: Sequence[Mapping[str, Any]]) -> int:
    """單一交易批次寫入評估紀錄；重複鍵 (symbol, evaluator, source, bar_ts) 忽略。"""
    if not rows:
        return 0
    params = [tuple(row.get(col) for col in LOG_COLUMNS) for row in rows]
    counts = await execute_write_many_async([(_INSERT_LOG_SQL, params, True)])
    return int(counts[0]) if counts else 0


def fetch_pending_evaluations(
    older_than_utc: str, limit: int = 300
) -> list[dict[str, Any]]:
    """讀取尚未標註、且評估時間早於 `older_than_utc` 的紀錄 (舊的優先)。"""
    conn = None
    try:
        conn = get_read_connection()
        conn.row_factory = _dict_factory
        cursor = conn.cursor()
        cursor.execute(
            """
            SELECT l.* FROM regime_evaluation_log l
            LEFT JOIN regime_evaluation_outcome o ON o.evaluation_id = l.id
            WHERE o.evaluation_id IS NULL AND l.evaluated_at <= ?
            ORDER BY l.evaluated_at ASC
            LIMIT ?
            """,
            (older_than_utc, int(limit)),
        )
        return list(cursor.fetchall())
    except Exception as e:
        logger.error(f"讀取待標註評估紀錄失敗: {e}")
        return []
    finally:
        if conn:
            conn.close()


async def save_evaluation_outcomes(outcomes: Sequence[Mapping[str, Any]]) -> int:
    if not outcomes:
        return 0
    params = [tuple(o.get(col) for col in OUTCOME_COLUMNS) for o in outcomes]
    counts = await execute_write_many_async([(_INSERT_OUTCOME_SQL, params, True)])
    return int(counts[0]) if counts else 0


async def purge_regime_evaluation_log(
    retention_days: int = 365, no_data_days: int = 30
) -> list[int]:
    """保留期清理。先刪 outcome 再刪 log (未啟用外鍵，無 cascade)。

    * 超過 `retention_days` 的紀錄連同其 outcome 一併刪除。
    * 標為 NO_DATA 超過 `no_data_days` 的 outcome 刪除 (log 保留，可再次嘗試標註
      ——但通常已超出 yfinance 15m/1h 回溯窗，實務上會再被標為 NO_DATA)。
    """
    cutoff = f"-{int(retention_days)} days"
    no_data_cutoff = f"-{int(no_data_days)} days"
    return await execute_write_many_async(
        [
            (
                "DELETE FROM regime_evaluation_outcome WHERE evaluation_id IN "
                "(SELECT id FROM regime_evaluation_log "
                "WHERE evaluated_at < datetime('now', ?))",
                (cutoff,),
            ),
            (
                "DELETE FROM regime_evaluation_log "
                "WHERE evaluated_at < datetime('now', ?)",
                (cutoff,),
            ),
            (
                "DELETE FROM regime_evaluation_outcome WHERE label_status = 'NO_DATA' "
                "AND labeled_at < datetime('now', ?)",
                (no_data_cutoff,),
            ),
        ]
    )


def _dict_factory(cursor: Any, row: Any) -> dict[str, Any]:
    return {col[0]: row[idx] for idx, col in enumerate(cursor.description)}


def count_labeled_outcomes(evaluator: Optional[str] = None) -> int:
    conn = None
    try:
        conn = get_read_connection()
        cursor = conn.cursor()
        if evaluator:
            cursor.execute(
                "SELECT COUNT(*) FROM regime_evaluation_outcome o "
                "JOIN regime_evaluation_log l ON l.id = o.evaluation_id "
                "WHERE o.label_status = 'LABELED' AND l.evaluator = ?",
                (evaluator,),
            )
        else:
            cursor.execute(
                "SELECT COUNT(*) FROM regime_evaluation_outcome "
                "WHERE label_status = 'LABELED'"
            )
        return int(cursor.fetchone()[0])
    except Exception as e:
        logger.error(f"統計已標註評估紀錄失敗: {e}")
        return 0
    finally:
        if conn:
            conn.close()
