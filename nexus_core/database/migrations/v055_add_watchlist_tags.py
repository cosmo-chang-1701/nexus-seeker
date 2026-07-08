version = 55
description = "新增 Watchlist Tags (標籤) 功能"
sql = """
            CREATE TABLE IF NOT EXISTS watchlist_tags (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                symbol TEXT NOT NULL,
                tag_name TEXT NOT NULL,
                UNIQUE(user_id, symbol, tag_name)
            );

            CREATE INDEX IF NOT EXISTS idx_watchlist_tags_user_symbol ON watchlist_tags(user_id, symbol);
            CREATE INDEX IF NOT EXISTS idx_watchlist_tags_tag_name ON watchlist_tags(tag_name);
        """
