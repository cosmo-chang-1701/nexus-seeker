from typing import Any
import sqlite3
import json

from database.connection import (
    execute_write,
    execute_write_rowcount,
    get_read_connection,
)

# ==========================================
# 現貨持倉 (Holdings) CRUD
# ==========================================


def add_holding(user_id: int, symbol: str, quantity: float, avg_cost: float) -> bool:
    """新增或更新現貨持倉"""
    symbol = symbol.upper()
    try:
        conn = get_read_connection()
        try:
            cursor = conn.cursor()
            cursor.execute(
                "SELECT id, metadata FROM assets WHERE user_id = ? AND symbol = ? AND context_type = 'HOLDING'",
                (user_id, symbol),
            )
            row = cursor.fetchone()
        finally:
            conn.close()

        metadata = {"quantity": quantity, "avg_cost": avg_cost}

        if row:
            # 更新已存在的紀錄（沿用既有 metadata 其餘欄位）
            existing_meta = json.loads(row[1]) if row[1] else {}
            existing_meta.update(metadata)
            execute_write(
                """
                UPDATE assets
                SET metadata = ?, updated_at = CURRENT_TIMESTAMP
                WHERE id = ?
            """,
                (json.dumps(existing_meta), row[0]),
            )
        else:
            execute_write(
                """
                INSERT INTO assets (user_id, symbol, context_type, metadata)
                VALUES (?, ?, 'HOLDING', ?)
            """,
                (user_id, symbol, json.dumps(metadata)),
            )
        return True
    except Exception:
        return False


def get_user_holdings(user_id: int) -> Any:
    """取得特定使用者的所有現貨持倉"""
    conn = get_read_connection()
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()
    try:
        cursor.execute(
            "SELECT id, symbol, metadata, created_at FROM assets WHERE user_id = ? AND context_type = 'HOLDING'",
            (user_id,),
        )
        rows = []
        for row in cursor.fetchall():
            d = dict(row)
            meta = json.loads(d["metadata"]) if d["metadata"] else {}
            d["quantity"] = meta.get("quantity", 0.0)
            d["avg_cost"] = meta.get("avg_cost", 0.0)
            d["weighted_delta"] = meta.get("weighted_delta", 0.0)
            d["asset_class"] = meta.get("asset_class")
            d["max_allocation_pct"] = meta.get("max_allocation_pct")
            d["target_allocation_pct"] = meta.get("target_allocation_pct")
            d["boxx_allocation_pct"] = meta.get("boxx_allocation_pct")
            d["acquired_at"] = meta.get("acquired_at")
            d["dynamic_strategy_state"] = meta.get("dynamic_strategy_state")
            rows.append(d)
        return rows
    finally:
        conn.close()


def delete_holding(user_id: int, symbol: str) -> bool:
    """刪除特定的現貨持倉"""
    return (
        execute_write_rowcount(
            "DELETE FROM assets WHERE user_id = ? AND symbol = ? AND context_type = 'HOLDING'",
            (user_id, symbol.upper()),
        )
        > 0
    )


def get_all_holdings() -> Any:
    """取得全站所有現貨持倉 (供背景任務使用)"""
    conn = get_read_connection()
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()
    try:
        cursor.execute(
            "SELECT id, user_id, symbol, metadata FROM assets WHERE context_type = 'HOLDING'"
        )
        rows = []
        for row in cursor.fetchall():
            d = dict(row)
            meta = json.loads(d["metadata"]) if d["metadata"] else {}
            d["quantity"] = meta.get("quantity", 0.0)
            d["avg_cost"] = meta.get("avg_cost", 0.0)
            d["asset_class"] = meta.get("asset_class")
            d["max_allocation_pct"] = meta.get("max_allocation_pct")
            d["target_allocation_pct"] = meta.get("target_allocation_pct")
            d["boxx_allocation_pct"] = meta.get("boxx_allocation_pct")
            d["acquired_at"] = meta.get("acquired_at")
            d["dynamic_strategy_state"] = meta.get("dynamic_strategy_state")
            rows.append(d)
        return rows
    finally:
        conn.close()


def update_holding_greeks(holding_id: int, weighted_delta: float) -> Any:
    """更新現貨持倉的加權 Delta"""
    conn = get_read_connection()
    try:
        cursor = conn.cursor()
        cursor.execute("SELECT metadata FROM assets WHERE id = ?", (holding_id,))
        row = cursor.fetchone()
    finally:
        conn.close()

    if row:
        meta = json.loads(row[0]) if row[0] else {}
        meta["weighted_delta"] = weighted_delta
        execute_write(
            "UPDATE assets SET metadata = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ?",
            (json.dumps(meta), holding_id),
        )
