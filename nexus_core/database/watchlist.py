import sqlite3
from config import DB_NAME

# ==========================================
# 觀察清單 (Watchlist) CRUD (綁定 user_id)
# ==========================================
def add_watchlist_symbol(user_id, symbol, use_llm=True):
    """將標的加入觀察清單"""
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()

    llm_flag = 1 if use_llm else 0
    try:
        cursor.execute('INSERT INTO watchlist (user_id, symbol, use_llm) VALUES (?, ?, ?)', (user_id, symbol, llm_flag))
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
    cursor.execute('SELECT symbol, use_llm FROM watchlist WHERE user_id = ?', (user_id,))
    rows = cursor.fetchall()
    conn.close()
    return rows

def get_user_watchlist_by_symbol(user_id, symbol):
    """取得特定使用者的單一觀察標的"""
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    cursor.execute('SELECT symbol, use_llm FROM watchlist WHERE user_id = ? AND symbol = ?', (user_id, symbol))
    rows = cursor.fetchall()
    conn.close()
    return rows

def update_user_watchlist(user_id, symbol, use_llm=None):
    """
    動態更新觀察清單的設定。
    """
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    
    updates = []
    params = []
    
    if use_llm is not None:
        updates.append("use_llm = ?")
        params.append(1 if use_llm else 0) 
        
    if not updates:
        conn.close()
        return False
        
    params.extend([user_id, symbol])
    query = f"UPDATE watchlist SET {', '.join(updates)} WHERE user_id = ? AND symbol = ?"
    
    cursor.execute(query, tuple(params))
    rows_affected = cursor.rowcount
    conn.commit()
    conn.close()
    
    return rows_affected > 0

def get_all_watchlist():
    """取得全站所有觀察清單 (供背景排程使用)"""
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    cursor.execute('SELECT user_id, symbol, use_llm FROM watchlist')
    rows = cursor.fetchall()
    conn.close()
    return rows # 格式: [(user_id, symbol, use_llm), ...]

def delete_watchlist_symbol(user_id, symbol):
    """將標的從觀察清單移除"""
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    cursor.execute('DELETE FROM watchlist WHERE user_id = ? AND symbol = ?', (user_id, symbol))
    changes = cursor.rowcount
    conn.commit()
    conn.close()
    return changes > 0


# ==========================================
# 訊號追蹤 (Anti-Whipsaw State) CRUD
# ==========================================
def get_watchlist_alert_state(user_id, symbol):
    """取得標的上一次觸發訊號的狀態快照"""
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    cursor.execute(
        'SELECT last_cross_dir, last_cross_price, last_cross_time '
        'FROM watchlist WHERE user_id = ? AND symbol = ?',
        (user_id, symbol)
    )
    row = cursor.fetchone()
    conn.close()

    if row is None or row[0] is None:
        return None

    return {
        'last_cross_dir': row[0],
        'last_cross_price': row[1],
        'last_cross_time': row[2],
    }


def update_watchlist_alert_state(user_id, symbol, direction, price, timestamp):
    """記錄本次觸發的訊號狀態"""
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    cursor.execute(
        'UPDATE watchlist '
        'SET last_cross_dir = ?, last_cross_price = ?, last_cross_time = ? '
        'WHERE user_id = ? AND symbol = ?',
        (direction, price, timestamp, user_id, symbol)
    )
    conn.commit()
    conn.close()
