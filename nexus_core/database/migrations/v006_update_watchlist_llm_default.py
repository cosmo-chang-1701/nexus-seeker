version = 6
description = "將 watchlist.use_llm 預設值改為 1 (包含資料庫欄位預設與現有資料更新)"
sql = """
    -- 1. 更新現有所有記錄，將 llm 設為 1 (啟用)
    UPDATE watchlist SET use_llm = 1;

    -- 2. 透過重建表格來修改 SQLite 的 DEFAULT 值 (SQLite 不支援 ALTER COLUMN)
    CREATE TABLE watchlist_new (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER NOT NULL,
        symbol TEXT NOT NULL,
        stock_cost REAL DEFAULT 0.0,
        use_llm INTEGER DEFAULT 1,
        UNIQUE(user_id, symbol)
    );

    INSERT INTO watchlist_new (id, user_id, symbol, stock_cost, use_llm)
    SELECT id, user_id, symbol, stock_cost, use_llm FROM watchlist;

    DROP TABLE watchlist;
    ALTER TABLE watchlist_new RENAME TO watchlist;
"""
