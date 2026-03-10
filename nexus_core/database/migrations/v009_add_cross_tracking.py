version = 9
description = "為 watchlist 表格新增 EMA 穿透訊號追蹤欄位 (防騙線機制)"
sql = """
    ALTER TABLE watchlist ADD COLUMN last_cross_dir TEXT;
    ALTER TABLE watchlist ADD COLUMN last_cross_price REAL;
    ALTER TABLE watchlist ADD COLUMN last_cross_time INTEGER;
"""
