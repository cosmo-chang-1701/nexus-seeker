import sqlite3
import json
import config

# ==========================================
# 現貨持倉 (Holdings) CRUD
# ==========================================


def add_holding(user_id: int, symbol: str, quantity: float, avg_cost: float) -> bool:
    """新增或更新現貨持倉"""
    conn = sqlite3.connect(config.DB_NAME)
    cursor = conn.cursor()
    symbol = symbol.upper()
    try:
        # 檢查是否已存在
        cursor.execute(
            "SELECT id, metadata FROM assets WHERE user_id = ? AND symbol = ? AND context_type = 'HOLDING'",
            (user_id, symbol),
        )
        row = cursor.fetchone()

        metadata = {"quantity": quantity, "avg_cost": avg_cost}

        if row:
            # 更新已存在的紀錄
            existing_meta = json.loads(row[1]) if row[1] else {}
            existing_meta.update(metadata)
            cursor.execute(
                """
                UPDATE assets
                SET metadata = ?, updated_at = CURRENT_TIMESTAMP
                WHERE id = ?
            """,
                (json.dumps(existing_meta), row[0]),
            )
        else:
            # 新增紀錄
            cursor.execute(
                """
                INSERT INTO assets (user_id, symbol, context_type, metadata)
                VALUES (?, ?, 'HOLDING', ?)
            """,
                (user_id, symbol, json.dumps(metadata)),
            )

        conn.commit()
        return True
    except Exception:
        return False
    finally:
        conn.close()


def get_user_holdings(user_id: int):
    """取得特定使用者的所有現貨持倉"""
    conn = sqlite3.connect(config.DB_NAME)
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()
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
        rows.append(d)
    conn.close()
    return rows


def delete_holding(user_id: int, symbol: str) -> bool:
    """刪除特定的現貨持倉"""
    conn = sqlite3.connect(config.DB_NAME)
    cursor = conn.cursor()
    cursor.execute(
        "DELETE FROM assets WHERE user_id = ? AND symbol = ? AND context_type = 'HOLDING'",
        (user_id, symbol.upper()),
    )
    changes = cursor.rowcount
    conn.commit()
    conn.close()
    return changes > 0


def get_all_holdings():
    """取得全站所有現貨持倉 (供背景任務使用)"""
    conn = sqlite3.connect(config.DB_NAME)
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()
    cursor.execute(
        "SELECT id, user_id, symbol, metadata FROM assets WHERE context_type = 'HOLDING'"
    )
    rows = []
    for row in cursor.fetchall():
        d = dict(row)
        meta = json.loads(d["metadata"]) if d["metadata"] else {}
        d["quantity"] = meta.get("quantity", 0.0)
        d["avg_cost"] = meta.get("avg_cost", 0.0)
        rows.append(d)
    conn.close()
    return rows


def update_holding_greeks(holding_id: int, weighted_delta: float):
    """更新現貨持倉的加權 Delta"""
    conn = sqlite3.connect(config.DB_NAME)
    cursor = conn.cursor()

    cursor.execute("SELECT metadata FROM assets WHERE id = ?", (holding_id,))
    row = cursor.fetchone()
    if row:
        meta = json.loads(row[0]) if row[0] else {}
        meta["weighted_delta"] = weighted_delta
        cursor.execute(
            "UPDATE assets SET metadata = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ?",
            (json.dumps(meta), holding_id),
        )

    conn.commit()
    conn.close()
