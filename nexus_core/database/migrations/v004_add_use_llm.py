version = 4
description = "新增 use_llm 欄位至 watchlist 資料表"
sql = """
            ALTER TABLE watchlist ADD COLUMN use_llm INTEGER DEFAULT 0;
        """