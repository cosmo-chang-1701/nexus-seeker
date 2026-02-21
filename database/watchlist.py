import sqlite3
from config import DB_NAME

# ==========================================
# 觀察清單 (Watchlist) CRUD (綁定 user_id)
# ==========================================
def add_watchlist_symbol(user_id, symbol, stock_cost=0.0):
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    try:
        cursor.execute('INSERT INTO watchlist (user_id, symbol, stock_cost) VALUES (?, ?, ?)', (user_id, symbol, stock_cost))
        conn.commit()
        success = True
    except sqlite3.IntegrityError:
        success = False # 該使用者已加入過該標的
    conn.close()
    return success

def get_user_watchlist(user_id):
    """取得特定使用者的觀察清單"""
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    cursor.execute('SELECT symbol FROM watchlist WHERE user_id = ?', (user_id,))
    rows = cursor.fetchall()
    conn.close()
    return [row[0] for row in rows]

def get_all_watchlist():
    """取得全站所有觀察清單 (供背景排程使用)"""
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    cursor.execute('SELECT user_id, symbol, stock_cost FROM watchlist')
    rows = cursor.fetchall()
    conn.close()
    return rows # 格式: [(user_id, symbol, stock_cost), ...]

def delete_watchlist_symbol(user_id, symbol):
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    cursor.execute('DELETE FROM watchlist WHERE user_id = ? AND symbol = ?', (user_id, symbol))
    changes = cursor.rowcount
    conn.commit()
    conn.close()
    return changes > 0
