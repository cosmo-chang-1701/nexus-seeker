version = 3
description = "刪除棄用的 is_covered 欄位"
sql = """
            ALTER TABLE portfolio DROP COLUMN is_covered;
            ALTER TABLE watchlist DROP COLUMN is_covered;
        """
