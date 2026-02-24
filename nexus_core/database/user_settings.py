import sqlite3
from config import DB_NAME

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
