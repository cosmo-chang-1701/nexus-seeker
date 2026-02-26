import sqlite3
import datetime
import json
from config import DB_NAME

# ==========================================
# 虛擬交易室 (Virtual Trading Room) CRUD
# ==========================================

def add_virtual_trade(user_id: int, symbol: str, opt_type: str, strike: float, expiry: str, entry_price: float, quantity: int, tags: list = None, parent_trade_id: int = None):
    tags_str = json.dumps(tags) if tags else None
    
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    cursor.execute('''
        INSERT INTO virtual_trades (user_id, symbol, opt_type, strike, expiry, entry_price, quantity, status, parent_trade_id, tags)
        VALUES (?, ?, ?, ?, ?, ?, ?, 'OPEN', ?, ?)
    ''', (user_id, symbol, opt_type, strike, expiry, entry_price, quantity, parent_trade_id, tags_str))
    
    trade_id = cursor.lastrowid
    conn.commit()
    conn.close()
    return trade_id

def get_virtual_trades(user_id: int = None, status: str = None):
    """
    獲取虛擬交易紀錄
    若傳入 user_id，則只過濾特定用戶
    若傳入 status，則只過濾特定狀態 (如 'OPEN', 'CLOSED', 'ROLLED')
    """
    conn = sqlite3.connect(DB_NAME)
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()
    
    query = "SELECT * FROM virtual_trades WHERE 1=1"
    params = []
    
    if user_id is not None:
        query += " AND user_id = ?"
        params.append(user_id)
        
    if status is not None:
        query += " AND status = ?"
        params.append(status)
        
    cursor.execute(query, tuple(params))
    rows = [dict(row) for row in cursor.fetchall()]
    conn.close()
    
    # deserialize tags
    for row in rows:
        if row['tags']:
            row['tags'] = json.loads(row['tags'])
        else:
            row['tags'] = []
            
    return rows

def get_all_open_virtual_trades():
    """獲取全站所有開啟的虛擬交易"""
    return get_virtual_trades(status='OPEN')

def get_virtual_trade_by_id(trade_id: int):
    """根據 trade_id 獲取虛擬交易"""
    conn = sqlite3.connect(DB_NAME)
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()
    cursor.execute("SELECT * FROM virtual_trades WHERE id = ?", (trade_id,))
    fetched = cursor.fetchone()
    
    row = None
    if fetched:
        row = dict(fetched)
        if row['tags']:
            row['tags'] = json.loads(row['tags'])
        else:
            row['tags'] = []
            
    conn.close()
    return row

def close_virtual_trade(trade_id: int, exit_price: float, status: str = 'CLOSED', pnl: float = 0.0):
    """平倉虛擬交易"""
    conn = sqlite3.connect(DB_NAME)
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()
    
    # 先取得目前的資料來計算 PnL
    cursor.execute("SELECT entry_price, quantity FROM virtual_trades WHERE id = ?", (trade_id,))
    trade = cursor.fetchone()
    if not trade:
        conn.close()
        return False
        
    entry_price = trade['entry_price']
    quantity = trade['quantity']
    
    # PnL 計算:如果是買方(quantity > 0)，則 PnL = (exit - entry) * quantity * 100
    # 但 quantity 這裡是指合約數，我們統一用 (exit - entry) * quantity * 100 嗎？
    # 退一步說，由於 quantity 可以帶正負號 (買方 > 0, 賣方 < 0)?
    # 或者用傳統方式: PnL = (exit_price - entry_price) * quantity * 100 
    # 不行，如果我們不知道 quantity 的正負，這裡需要明確定義。通常 Long = 正, Short = 負
    # 假設 quantity 是帶正負號的: PnL = (exit_price - entry_price) * quantity * 100 
    # 等一下，結算時如果是 Short，進入價格是 5.0，平倉價格是 2.0，獲利 3.0，PnL = (2.0 - 5.0) * (-1) * 100 = 300，正確。
    pnl = (exit_price - entry_price) * quantity * 100
    
    now = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    
    cursor.execute('''
        UPDATE virtual_trades
        SET status = ?, exit_price = ?, closed_at = ?, pnl = ?
        WHERE id = ?
    ''', (status, exit_price, now, pnl, trade_id))
    
    conn.commit()
    conn.close()
    return True

def get_open_virtual_trades(user_id: int = None):
    """
    抓取所有開放中的虛擬部位。如果 user_id 為 None，則抓取全系統部位 (用於背景排程)。
    """
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    
    query = "SELECT * FROM virtual_trades WHERE status = 'OPEN'"
    params = ()
    
    if user_id:
        query += " AND user_id = ?"
        params = (user_id,)
        
    cursor.execute(query, params)
    columns = [column[0] for column in cursor.description]
    return [dict(zip(columns, row)) for row in cursor.fetchall()]

def get_all_virtual_trades(user_id: int):
    """
    抓取該使用者的所有虛擬交易紀錄 (不限狀態)，用於績效統計。
    """
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    
    # 這裡不加 status 濾網，因為我們要算歷史總帳
    query = "SELECT * FROM virtual_trades WHERE user_id = ? ORDER BY opened_at DESC"
    cursor.execute(query, (user_id,))
    
    columns = [column[0] for column in cursor.description]
    return [dict(zip(columns, row)) for row in cursor.fetchall()]