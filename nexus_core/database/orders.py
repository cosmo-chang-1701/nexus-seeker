import sqlite3
import config


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
    conn = sqlite3.connect(config.DB_NAME)
    cursor = conn.cursor()
    cursor.execute(
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
    order_id = cursor.lastrowid
    conn.commit()
    conn.close()
    if order_id is None:
        raise ValueError("無法獲取待成交委託單寫入之 ID")
    return order_id


def get_user_active_orders(user_id: int) -> list:
    """取得特定使用者的所有待成交委託單"""
    conn = sqlite3.connect(config.DB_NAME)
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()
    cursor.execute(
        "SELECT * FROM active_orders WHERE user_id = ? ORDER BY created_at DESC",
        (user_id,),
    )
    rows = [dict(row) for row in cursor.fetchall()]
    conn.close()
    return rows


def get_all_active_orders() -> list:
    """取得全站所有待成交委託單"""
    conn = sqlite3.connect(config.DB_NAME)
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()
    cursor.execute("SELECT * FROM active_orders ORDER BY created_at DESC")
    rows = [dict(row) for row in cursor.fetchall()]
    conn.close()
    return rows


def delete_active_order(order_id: int) -> bool:
    """刪除委託單"""
    conn = sqlite3.connect(config.DB_NAME)
    cursor = conn.cursor()
    cursor.execute("DELETE FROM active_orders WHERE id = ?", (order_id,))
    changes = cursor.rowcount
    conn.commit()
    conn.close()
    return changes > 0


def update_active_order_price(
    order_id: int,
    new_price: float | None,
    new_quantity: float | None = None,
    new_side: str | None = None,
) -> bool:
    """更新委託單價格 (包含 limit_price, stop_price, trailing_value 等) 與可選的數量/方向"""
    conn = sqlite3.connect(config.DB_NAME)
    cursor = conn.cursor()

    side = new_side.upper() if new_side is not None else None

    # 允許只更新方向/數量 (new_price=None)
    if new_price is None:
        if new_quantity is None and side is None:
            conn.close()
            return False
        if new_quantity is not None and side is None:
            cursor.execute(
                """
                UPDATE active_orders
                SET quantity = ?,
                    updated_at = CURRENT_TIMESTAMP
                WHERE id = ?
                """,
                (new_quantity, order_id),
            )
        elif new_quantity is None and side is not None:
            cursor.execute(
                """
                UPDATE active_orders
                SET side = ?,
                    updated_at = CURRENT_TIMESTAMP
                WHERE id = ?
                """,
                (side, order_id),
            )
        else:
            cursor.execute(
                """
                UPDATE active_orders
                SET quantity = ?,
                    side = ?,
                    updated_at = CURRENT_TIMESTAMP
                WHERE id = ?
                """,
                (new_quantity, side, order_id),
            )

        changes = cursor.rowcount
        conn.commit()
        conn.close()
        return changes > 0

    if new_quantity is None and side is None:
        cursor.execute(
            """
            UPDATE active_orders
            SET limit_price = CASE WHEN order_type IN ('LIMIT', 'STOP_LIMIT') THEN ? ELSE limit_price END,
                stop_price = CASE WHEN order_type IN ('STOP', 'STOP_LIMIT') THEN ? ELSE stop_price END,
                trailing_value = CASE WHEN order_type IN ('TRAILING_STOP_USD', 'TRAILING_STOP_PCT') THEN ? ELSE trailing_value END,
                updated_at = CURRENT_TIMESTAMP
            WHERE id = ?
            """,
            (new_price, new_price, new_price, order_id),
        )
    elif new_quantity is not None and side is None:
        cursor.execute(
            """
            UPDATE active_orders
            SET limit_price = CASE WHEN order_type IN ('LIMIT', 'STOP_LIMIT') THEN ? ELSE limit_price END,
                stop_price = CASE WHEN order_type IN ('STOP', 'STOP_LIMIT') THEN ? ELSE stop_price END,
                trailing_value = CASE WHEN order_type IN ('TRAILING_STOP_USD', 'TRAILING_STOP_PCT') THEN ? ELSE trailing_value END,
                quantity = ?,
                updated_at = CURRENT_TIMESTAMP
            WHERE id = ?
            """,
            (new_price, new_price, new_price, new_quantity, order_id),
        )
    elif new_quantity is None and side is not None:
        cursor.execute(
            """
            UPDATE active_orders
            SET limit_price = CASE WHEN order_type IN ('LIMIT', 'STOP_LIMIT') THEN ? ELSE limit_price END,
                stop_price = CASE WHEN order_type IN ('STOP', 'STOP_LIMIT') THEN ? ELSE stop_price END,
                trailing_value = CASE WHEN order_type IN ('TRAILING_STOP_USD', 'TRAILING_STOP_PCT') THEN ? ELSE trailing_value END,
                side = ?,
                updated_at = CURRENT_TIMESTAMP
            WHERE id = ?
            """,
            (new_price, new_price, new_price, side, order_id),
        )
    else:
        cursor.execute(
            """
            UPDATE active_orders
            SET limit_price = CASE WHEN order_type IN ('LIMIT', 'STOP_LIMIT') THEN ? ELSE limit_price END,
                stop_price = CASE WHEN order_type IN ('STOP', 'STOP_LIMIT') THEN ? ELSE stop_price END,
                trailing_value = CASE WHEN order_type IN ('TRAILING_STOP_USD', 'TRAILING_STOP_PCT') THEN ? ELSE trailing_value END,
                quantity = ?,
                side = ?,
                updated_at = CURRENT_TIMESTAMP
            WHERE id = ?
            """,
            (new_price, new_price, new_price, new_quantity, side, order_id),
        )

    changes = cursor.rowcount
    conn.commit()
    conn.close()
    return changes > 0
