version = 11
description = "新增 trade_category 欄位用以區分投機部位與對沖部位 (SPECULATIVE/HEDGE)"
sql = """
    ALTER TABLE portfolio ADD COLUMN trade_category TEXT DEFAULT 'SPECULATIVE';
    ALTER TABLE virtual_trades ADD COLUMN trade_category TEXT DEFAULT 'SPECULATIVE';
"""
