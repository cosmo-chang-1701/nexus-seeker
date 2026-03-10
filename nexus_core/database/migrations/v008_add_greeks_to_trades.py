version = 8
description = "為 portfolio 與 virtual_trades 表格新增希臘字母 (weighted_delta, theta, gamma) 欄位"
sql = """
            -- 為真實持倉表格新增欄位
            ALTER TABLE portfolio ADD COLUMN weighted_delta REAL DEFAULT 0.0;
            ALTER TABLE portfolio ADD COLUMN theta REAL DEFAULT 0.0;
            ALTER TABLE portfolio ADD COLUMN gamma REAL DEFAULT 0.0;

            -- 為虛擬交易表格新增欄位
            ALTER TABLE virtual_trades ADD COLUMN weighted_delta REAL DEFAULT 0.0;
            ALTER TABLE virtual_trades ADD COLUMN theta REAL DEFAULT 0.0;
            ALTER TABLE virtual_trades ADD COLUMN gamma REAL DEFAULT 0.0;
        """
