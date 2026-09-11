import sqlite3
from typing import Any

from database.connection import (
    execute_write,
    execute_write_rowcount,
    get_read_connection,
)


def add_active_order(
    user_id: int,
    symbol: str,
    quantity: float,
    order_type: str,
    validity: str,
    side: str = "BUY",
    limit_price: float = 0.0,
    stop_price: float = 0.0,
    trailing_value: float = 0.0,
) -> int:
    """新增一個待成交委託單"""
    order_id = execute_write(
        """
        INSERT INTO active_orders (
            user_id, symbol, quantity, order_type, validity, side,
            limit_price, stop_price, trailing_value
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            user_id,
            symbol.upper(),
            quantity,
            order_type.upper(),
            validity.upper(),
            side.upper(),
            limit_price,
            stop_price,
            trailing_value,
        ),
    )
    if not isinstance(order_id, int):
        raise ValueError("無法獲取待成交委託單寫入之 ID")
    return order_id


def get_user_active_orders(user_id: int) -> list:
    """取得特定使用者的所有待成交委託單"""
    conn = get_read_connection()
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()
    try:
        cursor.execute(
            "SELECT * FROM active_orders WHERE user_id = ? ORDER BY created_at DESC",
            (user_id,),
        )
        rows = [dict(row) for row in cursor.fetchall()]
        return rows
    finally:
        conn.close()


def get_all_active_orders() -> list:
    """取得全站所有待成交委託單"""
    conn = get_read_connection()
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()
    try:
        cursor.execute("SELECT * FROM active_orders ORDER BY created_at DESC")
        rows = [dict(row) for row in cursor.fetchall()]
        return rows
    finally:
        conn.close()


def delete_active_order(order_id: int) -> bool:
    """刪除委託單"""
    return (
        execute_write_rowcount("DELETE FROM active_orders WHERE id = ?", (order_id,))
        > 0
    )


def update_active_order_price(
    order_id: int,
    new_price: float | None,
    new_quantity: float | None = None,
    new_side: str | None = None,
) -> bool:
    """更新委託單價格 (包含 limit_price, stop_price, trailing_value 等) 與可選的數量/方向"""
    side = new_side.upper() if new_side is not None else None

    # 先組出單一語句與參數，再走寫入佇列。改寫前每個分支各自 cursor.execute +
    # conn.commit()，且自開連線繞過 DatabaseWriteQueue。
    set_clauses: list[str] = []
    values: list[Any] = []

    if new_price is not None:
        set_clauses.extend(
            [
                "limit_price = CASE WHEN order_type IN ('LIMIT', 'STOP_LIMIT') THEN ? ELSE limit_price END",
                "stop_price = CASE WHEN order_type IN ('STOP', 'STOP_LIMIT') THEN ? ELSE stop_price END",
                "trailing_value = CASE WHEN order_type IN ('TRAILING_STOP_USD', 'TRAILING_STOP_PCT') THEN ? ELSE trailing_value END",
            ]
        )
        values.extend([new_price, new_price, new_price])

    if new_quantity is not None:
        set_clauses.append("quantity = ?")
        values.append(new_quantity)

    if side is not None:
        set_clauses.append("side = ?")
        values.append(side)

    if not set_clauses:
        # 允許只更新方向/數量 (new_price=None)，但三者全為 None 時無事可做
        return False

    set_clauses.append("updated_at = CURRENT_TIMESTAMP")
    values.append(order_id)

    query = f"UPDATE active_orders SET {', '.join(set_clauses)} WHERE id = ?"
    # nosemgrep: python.sqlalchemy.security.sqlalchemy-execute-raw-query.sqlalchemy-execute-raw-query
    return execute_write_rowcount(query, tuple(values)) > 0


def get_active_order(order_id: int) -> dict | None:
    """取得單一待成交委託單的詳細資料"""
    conn = get_read_connection()
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()
    try:
        cursor.execute("SELECT * FROM active_orders WHERE id = ?", (order_id,))
        row = cursor.fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def update_active_order_fields(order_id: int, **kwargs) -> bool:  # type: ignore
    """動態更新委託單的多個欄位"""
    if not kwargs:
        return False

    allowed_fields = {
        "symbol",
        "quantity",
        "order_type",
        "validity",
        "side",
        "limit_price",
        "stop_price",
        "trailing_value",
    }

    updates: list[str] = []
    values: list[Any] = []
    for k, v in kwargs.items():
        if k in allowed_fields and v is not None:
            updates.append(f"{k} = ?")
            if k in ("symbol", "order_type", "validity", "side") and isinstance(v, str):
                values.append(v.upper())
            else:
                values.append(v)

    if not updates:
        return False

    updates.append("updated_at = CURRENT_TIMESTAMP")

    query = f"UPDATE active_orders SET {', '.join(updates)} WHERE id = ?"
    values.append(order_id)

    return execute_write_rowcount(query, tuple(values)) > 0  # nosemgrep
