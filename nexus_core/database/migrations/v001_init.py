version = 1
description = "建立初始資料表 (portfolio, watchlist, user_settings)"
sql = """
            CREATE TABLE IF NOT EXISTS portfolio (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                symbol TEXT NOT NULL,
                opt_type TEXT NOT NULL,
                strike REAL NOT NULL,
                expiry TEXT NOT NULL,
                entry_price REAL NOT NULL,
                quantity INTEGER NOT NULL
            );
            CREATE TABLE IF NOT EXISTS watchlist (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                symbol TEXT NOT NULL,
                UNIQUE(user_id, symbol)
            );
            CREATE TABLE IF NOT EXISTS user_settings (
                user_id INTEGER PRIMARY KEY,
                portfolio_value REAL NOT NULL
            );
        """
