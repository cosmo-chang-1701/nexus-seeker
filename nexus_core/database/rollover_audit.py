"""動態轉倉引擎審計軌跡 (Rollover Audit Trail) CRUD。

系統目前僅提供交易建議，不代為執行券商下單，因此無法追蹤「建議後的實際成交結果」；
本模組記錄的是「系統實際推送給使用者的每一則轉倉建議」本身（scenario/action/標的/
時間戳），供使用者事後回顧「哪個時間點、系統對哪個標的、給出了什麼建議」，
作為問責與策略回溯依據，而非模擬真實的成交後績效歸因。
"""

import sqlite3
import logging
from typing import Any, List, Optional

import config

logger = logging.getLogger(__name__)

# 單次查詢回傳上限，避免長期使用者的歷史紀錄一次性撐爆 Discord Embed 字元上限
_DEFAULT_QUERY_LIMIT = 20


async def log_rollover_instruction(
    user_id: int,
    symbol: str,
    scenario: str,
    action: str,
    sell_ratio: float,
    target_core: Optional[str] = None,
    suggested_price: Optional[str] = None,
    cash_impact: Optional[str] = None,
) -> None:
    """記錄一筆已實際推送給使用者的轉倉建議。失敗時僅記錄 log，不中斷主流程。"""
    conn = None
    try:
        conn = sqlite3.connect(config.DB_NAME)
        cursor = conn.cursor()
        cursor.execute(
            """
            INSERT INTO rollover_audit_log
                (user_id, symbol, scenario, action, sell_ratio, target_core,
                 suggested_price, cash_impact)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                user_id,
                symbol.upper(),
                scenario,
                action,
                sell_ratio,
                target_core,
                suggested_price,
                cash_impact,
            ),
        )
        conn.commit()
    except Exception as e:
        logger.error(f"寫入轉倉審計紀錄失敗 (uid={user_id}, symbol={symbol}): {e}")
    finally:
        if conn:
            conn.close()


def get_rollover_audit_log(
    user_id: int, limit: int = _DEFAULT_QUERY_LIMIT
) -> List[dict[str, Any]]:
    """取得指定使用者最近的轉倉建議推送紀錄，依時間新到舊排序。"""
    conn = None
    results: List[dict[str, Any]] = []
    try:
        conn = sqlite3.connect(config.DB_NAME)
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()
        cursor.execute(
            """
            SELECT symbol, scenario, action, sell_ratio, target_core,
                   suggested_price, cash_impact, created_at
            FROM rollover_audit_log
            WHERE user_id = ?
            ORDER BY created_at DESC
            LIMIT ?
            """,
            (user_id, limit),
        )
        results = [dict(row) for row in cursor.fetchall()]
    except Exception as e:
        logger.error(f"讀取轉倉審計紀錄失敗 (uid={user_id}): {e}")
    finally:
        if conn:
            conn.close()
    return results


__all__: list[str] = [
    "log_rollover_instruction",
    "get_rollover_audit_log",
]
