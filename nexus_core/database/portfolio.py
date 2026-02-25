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
    [Database Layer] 結算使用者當前投資組合的總體風險數據。
    """
    # 1. 從資料庫撈出所有持倉 (假設狀態為 active)
    # query = "SELECT symbol, opt_type, strike, expiry, quantity, stock_cost FROM trades WHERE user_id = ?"
    rows = get_active_trades_from_db(user_id) 
    
    if not rows:
        return {"total_weighted_delta": 0.0, "total_gamma": 0.0, "spy_price": get_current_spy_price()}

    # 🚀 取得基準 SPY 價格 (用於後續所有計算的基準)
    try:
        spy_df = yf.Ticker("SPY").history(period="1d")
        spy_price = spy_df['Close'].iloc[-1]
    except:
        spy_price = 500.0

    total_delta = 0.0
    total_gamma = 0.0
    total_theta = 0.0

    # 2. 遍歷所有持倉進行加權加總 (這部分邏輯與您剛才的 Orchestrator 相同)
    for row in rows:
        symbol, opt_type, strike, expiry, qty, stock_cost = row
        # ... (這裡執行 BSM 計算得出當前 delta, gamma, theta) ...
        # ... (計算 beta) ...
        
        # 進行 Beta 加權
        weight_factor = beta * (current_price / spy_price)
        total_delta += (curr_delta * qty * 100) * weight_factor
        total_gamma += (curr_gamma * qty * 100) * (weight_factor ** 2)

    return {
        "total_weighted_delta": total_delta,
        "total_gamma": total_gamma,
        "spy_price": spy_price,
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
