import sqlite3
from config import DB_NAME

def init_db():
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    
    # 加入 user_id 欄位
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS portfolio (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            symbol TEXT NOT NULL,
            opt_type TEXT NOT NULL,
            strike REAL NOT NULL,
            expiry TEXT NOT NULL,
            entry_price REAL NOT NULL,
            quantity INTEGER NOT NULL
        )
    ''')
    
    # 加入 user_id 欄位，並設定複合唯一鍵
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS watchlist (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            symbol TEXT NOT NULL,
            UNIQUE(user_id, symbol)
        )
    ''')
    
    conn.commit()
    conn.close()

# ==========================================
# 交易持倉 (Portfolio) CRUD (綁定 user_id)
# ==========================================
def add_portfolio_record(user_id, symbol, opt_type, strike, expiry, entry_price, quantity):
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    cursor.execute('''
        INSERT INTO portfolio (user_id, symbol, opt_type, strike, expiry, entry_price, quantity)
        VALUES (?, ?, ?, ?, ?, ?, ?)
    ''', (user_id, symbol, opt_type, strike, expiry, entry_price, quantity))
    trade_id = cursor.lastrowid
    conn.commit()
    conn.close()
    return trade_id

def get_user_portfolio(user_id):
    """取得特定使用者的持倉"""
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    cursor.execute('SELECT id, symbol, opt_type, strike, expiry, entry_price, quantity FROM portfolio WHERE user_id = ?', (user_id,))
    rows = cursor.fetchall()
    conn.close()
    return rows

def get_all_portfolio():
    """取得全站所有持倉 (供背景排程使用)"""
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    cursor.execute('SELECT user_id, id, symbol, opt_type, strike, expiry, entry_price, quantity FROM portfolio')
    rows = cursor.fetchall()
    conn.close()
    return rows

def delete_portfolio_record(user_id, trade_id):
    """確保使用者只能刪除自己的紀錄"""
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    cursor.execute('SELECT symbol, strike, opt_type FROM portfolio WHERE id = ? AND user_id = ?', (trade_id, user_id))
    record = cursor.fetchone()
    if record:
        cursor.execute('DELETE FROM portfolio WHERE id = ?', (trade_id,))
        conn.commit()
    conn.close()
    return record

# ==========================================
# 觀察清單 (Watchlist) CRUD (綁定 user_id)
# ==========================================
def add_watchlist_symbol(user_id, symbol):
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    try:
        cursor.execute('INSERT INTO watchlist (user_id, symbol) VALUES (?, ?)', (user_id, symbol))
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
    cursor.execute('SELECT user_id, symbol FROM watchlist')
    rows = cursor.fetchall()
    conn.close()
    return rows # 格式: [(user_id, symbol), ...]

def delete_watchlist_symbol(user_id, symbol):
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    cursor.execute('DELETE FROM watchlist WHERE user_id = ? AND symbol = ?', (user_id, symbol))
    changes = cursor.rowcount
    conn.commit()
    conn.close()
    return changes > 0