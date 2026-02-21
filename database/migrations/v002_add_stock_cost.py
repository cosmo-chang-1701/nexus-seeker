version = 2
description = "新增 stock_cost 欄位以支援 Covered Call 精確計算"
sql = """
            ALTER TABLE portfolio ADD COLUMN stock_cost REAL DEFAULT 0.0;
            ALTER TABLE watchlist ADD COLUMN stock_cost REAL DEFAULT 0.0;
        """
