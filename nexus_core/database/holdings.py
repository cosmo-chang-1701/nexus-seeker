import sqlite3
from config import DB_NAME

# ==========================================
# 現貨持倉 (Holdings) CRUD
# ==========================================

def add_holding(user_id: int, symbol: str, quantity: float, avg_cost: float) -> bool:
    """新增或更新現貨持倉"""
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    try:
        # 檢查是否已存在
        cursor.execute('SELECT id FROM holdings WHERE user_id = ? AND symbol = ?', (user_id, symbol))
        row = cursor.fetchone()
        
        if row:
            # 更新已存在的紀錄
            cursor.execute('''
                UPDATE holdings 
                SET quantity = ?, avg_cost = ? 
                WHERE user_id = ? AND symbol = ?
            ''', (quantity, avg_cost, user_id, symbol))
        else:
            # 新增紀錄
            cursor.execute('''
                INSERT INTO holdings (user_id, symbol, quantity, avg_cost) 
                VALUES (?, ?, ?, ?)
            ''', (user_id, symbol, quantity, avg_cost))
            
        conn.commit()
        return True
    except Exception:
        return False
    finally:
        conn.close()

def get_user_holdings(user_id: int):
    """取得特定使用者的所有現貨持倉"""
    conn = sqlite3.connect(DB_NAME)
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()
    cursor.execute('SELECT id, symbol, quantity, avg_cost, weighted_delta, created_at FROM holdings WHERE user_id = ?', (user_id,))
    rows = [dict(row) for row in cursor.fetchall()]
    conn.close()
    return rows

def delete_holding(user_id: int, symbol: str) -> bool:
    """刪除特定的現貨持倉"""
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    cursor.execute('DELETE FROM holdings WHERE user_id = ? AND symbol = ?', (user_id, symbol))
    changes = cursor.rowcount
    conn.commit()
    conn.close()
    return changes > 0

def get_all_holdings():
    """取得全站所有現貨持倉 (供背景任務使用)"""
    conn = sqlite3.connect(DB_NAME)
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()
    cursor.execute('SELECT id, user_id, symbol, quantity, avg_cost FROM holdings')
    rows = [dict(row) for row in cursor.fetchall()]
    conn.close()
    return rows

def update_holding_greeks(holding_id: int, weighted_delta: float):
    """更新現貨持倉的加權 Delta"""
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    cursor.execute('UPDATE holdings SET weighted_delta = ? WHERE id = ?', (weighted_delta, holding_id))
    conn.commit()
    conn.close()
