import sqlite3
from config import DB_NAME
import logging

logger = logging.getLogger(__name__)

def init_db():
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    
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
    
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS watchlist (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            symbol TEXT NOT NULL,
            UNIQUE(user_id, symbol)
        )
    ''')

    cursor.execute('''
        CREATE TABLE IF NOT EXISTS user_settings (
            user_id INTEGER PRIMARY KEY,
            portfolio_value REAL NOT NULL
        )
    ''')
    
    conn.commit()
    conn.close()
    logger.info("Database initialized successfully.")

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

# ==========================================
# 使用者設定檔 (User Settings) CRUD
# ==========================================
def set_user_capital(user_id, capital):
    """設定或更新使用者的總作戰資金"""
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    # 使用 UPSERT 語法，如果 user_id 已存在就更新，不存在就新增
    cursor.execute('''
        INSERT INTO user_settings (user_id, portfolio_value) 
        VALUES (?, ?)
        ON CONFLICT(user_id) DO UPDATE SET portfolio_value = excluded.portfolio_value
    ''', (user_id, capital))
    conn.commit()
    conn.close()

def get_user_capital(user_id):
    """取得使用者設定的總資金，若未設定則預設為 100000"""
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    cursor.execute('SELECT portfolio_value FROM user_settings WHERE user_id = ?', (user_id,))
    row = cursor.fetchone()
    conn.close()
    return row[0] if row else 100000.0

def get_all_user_ids():
    """取得資料庫中所有出現過的使用者 ID"""
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    
    # 使用 UNION 此處會自動去重
    cursor.execute('''
        SELECT user_id FROM portfolio
        UNION
        SELECT user_id FROM watchlist
        UNION
        SELECT user_id FROM user_settings
    ''')
    
    rows = cursor.fetchall()
    conn.close()
    return [row[0] for row in rows]