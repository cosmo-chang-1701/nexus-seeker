import sqlite3
from config import DB_NAME

# ==========================================
# 觀察清單 (Watchlist) CRUD (綁定 user_id)
# ==========================================
def add_watchlist_symbol(user_id, symbol, stock_cost=0.0, use_llm=True):
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()

    llm_flag = 1 if use_llm else 0
    try:
        cursor.execute('INSERT INTO watchlist (user_id, symbol, stock_cost, use_llm) VALUES (?, ?, ?, ?)', (user_id, symbol, stock_cost, llm_flag))
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
    cursor.execute('SELECT symbol, stock_cost, use_llm FROM watchlist WHERE user_id = ?', (user_id,))
    rows = cursor.fetchall()
    conn.close()
    return rows

def get_user_watchlist_by_symbol(user_id, symbol):
    """取得特定使用者的觀察清單"""
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    cursor.execute('SELECT symbol, stock_cost, use_llm FROM watchlist WHERE user_id = ? AND symbol = ?', (user_id, symbol))
    rows = cursor.fetchall()
    conn.close()
    return rows

def update_user_watchlist(user_id, symbol, stock_cost=None, use_llm=None):
    """
    動態更新觀察清單的設定。
    只更新有傳入值 (不為 None) 的欄位。
    """
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    
    updates = []
    params = []
    
    # 動態組裝 SQL 語法
    if stock_cost is not None:
        updates.append("stock_cost = ?")
        params.append(stock_cost)
        
    if use_llm is not None:
        updates.append("use_llm = ?")
        params.append(1 if use_llm else 0) # 轉為 SQLite 支援的整數
        
    if not updates:
        conn.close()
        return False # 沒有需要更新的欄位
        
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
    cursor.execute('SELECT user_id, symbol, stock_cost, use_llm FROM watchlist')
    rows = cursor.fetchall()
    conn.close()
    return rows # 格式: [(user_id, symbol, stock_cost, use_llm), ...]

def delete_watchlist_symbol(user_id, symbol):
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
    """
    取得特定標的上一次觸發 CROSSOVER 訊號的狀態快照。

    Returns:
        dict | None: 包含 last_cross_dir, last_cross_price, last_cross_time 的字典。
                     若該標的從未觸發過訊號，回傳 None。
    """
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
    """
    記錄本次觸發的 CROSSOVER 訊號狀態，供下一輪防騙線比對使用。

    Args:
        user_id: Discord 使用者 ID。
        symbol: 標的代號。
        direction: 穿透方向 ("BULLISH" 或 "BEARISH")。
        price: 觸發當下的現價。
        timestamp: Unix Timestamp (int)。
    """
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
