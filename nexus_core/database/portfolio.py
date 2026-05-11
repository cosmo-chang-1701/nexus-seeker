import sqlite3
import json
import config


# ==========================================
# 交易持倉 (Portfolio) CRUD (綁定 user_id)
# ==========================================
def add_portfolio_record(
    user_id,
    symbol,
    opt_type,
    strike,
    expiry,
    entry_price,
    quantity,
    stock_cost,
    weighted_delta: float = 0.0,
    theta: float = 0.0,
    gamma: float = 0.0,
    trade_category: str = "SPECULATIVE",
):
    conn = sqlite3.connect(config.DB_NAME)
    cursor = conn.cursor()

    metadata = {
        "opt_type": opt_type,
        "strike": strike,
        "expiry": expiry,
        "entry_price": entry_price,
        "quantity": quantity,
        "stock_cost": stock_cost,
        "weighted_delta": weighted_delta,
        "theta": theta,
        "gamma": gamma,
        "category": trade_category,
    }

    cursor.execute(
        """
        INSERT INTO assets (user_id, symbol, context_type, metadata)
        VALUES (?, ?, 'TRADE', ?)
    """,
        (user_id, symbol.upper(), json.dumps(metadata)),
    )
    trade_id = cursor.lastrowid
    conn.commit()
    conn.close()
    return trade_id


def get_user_portfolio(user_id):
    """取得特定使用者的持倉"""
    conn = sqlite3.connect(config.DB_NAME)
    cursor = conn.cursor()
    cursor.execute(
        "SELECT id, symbol, metadata FROM assets WHERE user_id = ? AND context_type = 'TRADE'",
        (user_id,),
    )
    rows = []
    for row in cursor.fetchall():
        asset_id, sym, meta_json = row
        m = json.loads(meta_json) if meta_json else {}
        rows.append(
            (
                asset_id,
                sym,
                m.get("opt_type"),
                m.get("strike"),
                m.get("expiry"),
                m.get("entry_price"),
                m.get("quantity"),
                m.get("stock_cost", 0.0),
                m.get("weighted_delta", 0.0),
                m.get("theta", 0.0),
                m.get("gamma", 0.0),
                m.get("category", "SPECULATIVE"),
            )
        )
    conn.close()
    return rows


def get_all_portfolio():
    """取得全站所有持倉 (供背景排程使用)"""
    conn = sqlite3.connect(config.DB_NAME)
    cursor = conn.cursor()
    cursor.execute(
        "SELECT user_id, id, symbol, metadata FROM assets WHERE context_type = 'TRADE'"
    )
    rows = []
    for row in cursor.fetchall():
        uid, asset_id, sym, meta_json = row
        m = json.loads(meta_json) if meta_json else {}
        rows.append(
            (
                uid,
                asset_id,
                sym,
                m.get("opt_type"),
                m.get("strike"),
                m.get("expiry"),
                m.get("entry_price"),
                m.get("quantity"),
                m.get("stock_cost", 0.0),
                m.get("weighted_delta", 0.0),
                m.get("theta", 0.0),
                m.get("gamma", 0.0),
                m.get("category", "SPECULATIVE"),
            )
        )
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
    return {"total_weighted_delta": 0.0, "total_gamma": 0.0, "active_count": len(rows)}


def delete_portfolio_record(user_id, trade_id):
    """確保使用者只能刪除自己的紀錄"""
    conn = sqlite3.connect(config.DB_NAME)
    cursor = conn.cursor()
    cursor.execute(
        "SELECT symbol, metadata FROM assets WHERE id = ? AND user_id = ? AND context_type = 'TRADE'",
        (trade_id, user_id),
    )
    row = cursor.fetchone()
    record = None
    if row:
        sym, meta_json = row
        m = json.loads(meta_json) if meta_json else {}
        record = (sym, m.get("strike"), m.get("opt_type"))
        cursor.execute("DELETE FROM assets WHERE id = ?", (trade_id,))
        conn.commit()
    conn.close()
    return record


def update_portfolio_greeks(
    trade_id: int, weighted_delta: float, theta: float, gamma: float
):
    """更新持倉紀錄的希臘字母數據"""
    conn = sqlite3.connect(config.DB_NAME)
    cursor = conn.cursor()

    cursor.execute("SELECT metadata FROM assets WHERE id = ?", (trade_id,))
    row = cursor.fetchone()
    if row:
        meta = json.loads(row[0]) if row[0] else {}
        meta["weighted_delta"] = weighted_delta
        meta["theta"] = theta
        meta["gamma"] = gamma
        cursor.execute(
            "UPDATE assets SET metadata = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ?",
            (json.dumps(meta), trade_id),
        )

    conn.commit()
    conn.close()
    return True


def is_symbol_in_portfolio(user_id: int, symbol: str) -> bool:
    """檢查標的是否存在於使用者的活躍持倉 (TRADE) 或現貨 (HOLDING) 中"""
    conn = sqlite3.connect(config.DB_NAME)
    cursor = conn.cursor()
    cursor.execute(
        """
        SELECT 1 FROM assets
        WHERE user_id = ? AND symbol = ? AND context_type IN ('TRADE', 'HOLDING')
        LIMIT 1
    """,
        (user_id, symbol.upper()),
    )
    res = cursor.fetchone()
    conn.close()
    return res is not None


# ==========================================
# 對沖歷史紀錄 (Hedge History)
# ==========================================
def add_hedge_history(user_id, date, alpha_pnl, hedge_pnl, effectiveness, tau_applied):
    """紀錄每日對沖績效與使用的 Tau 係數"""
    conn = sqlite3.connect(config.DB_NAME)
    cursor = conn.cursor()
    cursor.execute(
        """
        INSERT INTO hedge_history (user_id, date, alpha_pnl, hedge_pnl, effectiveness, tau_applied)
        VALUES (?, ?, ?, ?, ?, ?)
    """,
        (user_id, date, alpha_pnl, hedge_pnl, effectiveness, tau_applied),
    )
    conn.commit()
    conn.close()


def get_hedge_history(user_id, limit=7):
    """獲取過去 N 天的對沖績效紀錄"""
    conn = sqlite3.connect(config.DB_NAME)
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()
    cursor.execute(
        """
        SELECT date, alpha_pnl, hedge_pnl, effectiveness, tau_applied
        FROM hedge_history
        WHERE user_id = ?
        ORDER BY date DESC
        LIMIT ?
    """,
        (user_id, limit),
    )
    rows = cursor.fetchall()
    conn.close()
    # 返回時按日期升序 (從舊到新)，利於後續移動平均計算
    return [dict(row) for row in rows][::-1]
