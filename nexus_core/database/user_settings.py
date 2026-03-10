import sqlite3
import logging
from config import DB_NAME

logger = logging.getLogger(__name__)

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

def get_user_risk_limit(user_id: int) -> float:
    """
    從資料庫獲取使用者的個人化風險上限 (Base Risk Limit %)
    """
    try:
        with sqlite3.connect(DB_NAME) as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT risk_limit_pct FROM users WHERE user_id = ?", (user_id,))
            row = cursor.fetchone()
            
            # 如果使用者存在且欄位有值，回傳該值；否則回傳預設 15.0
            return float(row[0]) if row and row[0] is not None else 15.0
    except Exception as e:
        logger.error(f"無法讀取使用者 {user_id} 的風險限制: {e}")
        return 15.0

def update_user_risk_limit(user_id: int, new_limit: float) -> bool:
    """
    更新使用者的風險上限
    """
    try:
        # 限制範圍在 1% ~ 50% 之間，避免極端數值導致系統崩潰
        sanitized_limit = max(1.0, min(new_limit, 50.0))
        
        with sqlite3.connect(DB_NAME) as conn:
            cursor = conn.cursor()
            cursor.execute("""
                UPDATE users 
                SET risk_limit_pct = ? 
                WHERE user_id = ?
            """, (sanitized_limit, user_id))
            conn.commit()
            return True
    except Exception as e:
        logger.error(f"更新使用者 {user_id} 風險限制失敗: {e}")
        return False
