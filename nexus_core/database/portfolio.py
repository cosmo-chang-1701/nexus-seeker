import sqlite3
from config import DB_NAME

# ==========================================
# 交易持倉 (Portfolio) CRUD (綁定 user_id)
# ==========================================
def add_portfolio_record(user_id, symbol, opt_type, strike, expiry, entry_price, quantity, stock_cost):
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    cursor.execute('''
        INSERT INTO portfolio (user_id, symbol, opt_type, strike, expiry, entry_price, quantity, stock_cost)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
    ''', (user_id, symbol, opt_type, strike, expiry, entry_price, quantity, stock_cost))
    trade_id = cursor.lastrowid
    conn.commit()
    conn.close()
    return trade_id

def get_user_portfolio(user_id):
    """取得特定使用者的持倉"""
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    cursor.execute('SELECT id, symbol, opt_type, strike, expiry, entry_price, quantity, stock_cost FROM portfolio WHERE user_id = ?', (user_id,))
    rows = cursor.fetchall()
    conn.close()
    return rows

def get_all_portfolio():
    """取得全站所有持倉 (供背景排程使用)"""
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    cursor.execute('SELECT user_id, id, symbol, opt_type, strike, expiry, entry_price, quantity, stock_cost FROM portfolio')
    rows = cursor.fetchall()
    conn.close()
    return rows

def get_user_portfolio_stats(user_id):
    """
    [Database Layer] 結算使用者當前投資組合的總體風險數據 (暫行簡化版)。
    """
    rows = get_user_portfolio(user_id)
    
    if not rows:
        return {"total_weighted_delta": 0.0, "total_gamma": 0.0, "active_count": 0}

    # 目前僅回傳基礎結構，實際 Greeks 計算建議在 market_analysis 層次處理
    # 這裡預設回傳 0.0 以避免 trading.py 在整合時崩潰
    return {
        "total_weighted_delta": 0.0,
        "total_gamma": 0.0,
        "active_count": len(rows)
    }

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
