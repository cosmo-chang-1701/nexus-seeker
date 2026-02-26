version = 5
description = "建立 virtual_trades 表以支援虛擬交易室 (VTR)"
sql = """
            CREATE TABLE IF NOT EXISTS virtual_trades (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                symbol TEXT NOT NULL,
                opt_type TEXT NOT NULL,
                strike REAL NOT NULL,
                expiry TEXT NOT NULL,
                entry_price REAL NOT NULL,
                quantity INTEGER NOT NULL,
                status TEXT NOT NULL DEFAULT 'OPEN',
                opened_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                closed_at TIMESTAMP,
                pnl REAL,
                parent_trade_id INTEGER,
                exit_price REAL,
                tags TEXT
            );
        """
