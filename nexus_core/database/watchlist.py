import sqlite3
import json
import config


# ==========================================
# 觀察清單 (Watchlist) CRUD (綁定 user_id)
# ==========================================
def add_watchlist_symbol(user_id, symbol, use_llm=True):
    """將標的加入觀察清單"""
    conn = sqlite3.connect(config.DB_NAME)
    cursor = conn.cursor()

    metadata = json.dumps({"use_llm": bool(use_llm)})
    try:
        cursor.execute(
            "INSERT INTO assets (user_id, symbol, context_type, metadata) VALUES (?, ?, 'WATCH', ?)",
            (user_id, symbol.upper(), metadata),
        )
        conn.commit()
        success = True
    except sqlite3.IntegrityError:
        success = False  # 該使用者已加入過該標的
    conn.close()
    return success


def get_user_watchlist(user_id):
    """取得特定使用者的觀察清單"""
    conn = sqlite3.connect(config.DB_NAME)
    cursor = conn.cursor()
    cursor.execute(
        "SELECT symbol, metadata FROM assets WHERE user_id = ? AND context_type = 'WATCH'",
        (user_id,),
    )
    rows = []
    for sym, meta_json in cursor.fetchall():
        meta = json.loads(meta_json) if meta_json else {}
        use_llm = 1 if meta.get("use_llm", True) else 0
        rows.append((sym, use_llm))
    conn.close()
    return rows


def get_user_watchlist_by_symbol(user_id, symbol):
    """取得特定使用者的單一觀察標的"""
    conn = sqlite3.connect(config.DB_NAME)
    cursor = conn.cursor()
    cursor.execute(
        "SELECT symbol, metadata FROM assets WHERE user_id = ? AND symbol = ? AND context_type = 'WATCH'",
        (user_id, symbol.upper()),
    )
    rows = []
    for sym, meta_json in cursor.fetchall():
        meta = json.loads(meta_json) if meta_json else {}
        use_llm = 1 if meta.get("use_llm", True) else 0
        rows.append((sym, use_llm))
    conn.close()
    return rows


def update_user_watchlist(user_id, symbol, use_llm=None):
    """
    動態更新觀察清單的設定。
    """
    if use_llm is None:
        return False

    conn = sqlite3.connect(config.DB_NAME)
    cursor = conn.cursor()

    # 先獲取現有 metadata
    cursor.execute(
        "SELECT metadata FROM assets WHERE user_id = ? AND symbol = ? AND context_type = 'WATCH'",
        (user_id, symbol.upper()),
    )
    row = cursor.fetchone()
    if not row:
        conn.close()
        return False

    meta = json.loads(row[0]) if row[0] else {}
    meta["use_llm"] = bool(use_llm)

    cursor.execute(
        "UPDATE assets SET metadata = ?, updated_at = CURRENT_TIMESTAMP WHERE user_id = ? AND symbol = ? AND context_type = 'WATCH'",
        (json.dumps(meta), user_id, symbol.upper()),
    )
    rows_affected = cursor.rowcount
    conn.commit()
    conn.close()

    return rows_affected > 0


def get_all_watchlist():
    """取得全站所有觀察清單 (供背景排程使用)"""
    conn = sqlite3.connect(config.DB_NAME)
    cursor = conn.cursor()
    cursor.execute(
        "SELECT user_id, symbol, metadata FROM assets WHERE context_type = 'WATCH'"
    )
    rows = []
    for uid, sym, meta_json in cursor.fetchall():
        meta = json.loads(meta_json) if meta_json else {}
        use_llm = 1 if meta.get("use_llm", True) else 0
        rows.append((uid, sym, use_llm))
    conn.close()
    return rows  # 格式: [(user_id, symbol, use_llm), ...]


def delete_watchlist_symbol(user_id, symbol):
    """將標的從觀察清單移除"""
    conn = sqlite3.connect(config.DB_NAME)
    cursor = conn.cursor()
    cursor.execute(
        "DELETE FROM assets WHERE user_id = ? AND symbol = ? AND context_type = 'WATCH'",
        (user_id, symbol.upper()),
    )
    changes = cursor.rowcount
    conn.commit()
    conn.close()
    return changes > 0


# ==========================================
# 訊號追蹤 (Anti-Whipsaw State) CRUD
# ==========================================
def get_watchlist_alert_state(user_id, symbol):
    """取得標的上一次觸發訊號的狀態快照"""
    conn = sqlite3.connect(config.DB_NAME)
    cursor = conn.cursor()
    cursor.execute(
        "SELECT metadata FROM assets WHERE user_id = ? AND symbol = ? AND context_type = 'WATCH'",
        (user_id, symbol.upper()),
    )
    row = cursor.fetchone()
    conn.close()

    if row is None or row[0] is None:
        return None

    meta = json.loads(row[0])
    if "last_cross_dir" not in meta:
        return None

    return {
        "last_cross_dir": meta.get("last_cross_dir"),
        "last_cross_price": meta.get("last_cross_price"),
        "last_cross_time": meta.get("last_cross_time"),
    }


def update_watchlist_alert_state(user_id, symbol, direction, price, timestamp):
    """記錄本次觸發的訊號狀態"""
    conn = sqlite3.connect(config.DB_NAME)
    cursor = conn.cursor()

    # 先獲取現有 metadata
    cursor.execute(
        "SELECT metadata FROM assets WHERE user_id = ? AND symbol = ? AND context_type = 'WATCH'",
        (user_id, symbol.upper()),
    )
    row = cursor.fetchone()
    if not row:
        conn.close()
        return False

    meta = json.loads(row[0]) if row[0] else {}
    meta["last_cross_dir"] = direction
    meta["last_cross_price"] = price
    meta["last_cross_time"] = timestamp

    cursor.execute(
        "UPDATE assets SET metadata = ?, updated_at = CURRENT_TIMESTAMP WHERE user_id = ? AND symbol = ? AND context_type = 'WATCH'",
        (json.dumps(meta), user_id, symbol.upper()),
    )
    conn.commit()
    conn.close()
