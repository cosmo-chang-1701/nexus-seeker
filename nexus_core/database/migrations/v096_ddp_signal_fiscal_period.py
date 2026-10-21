version = 96
description = "ddp_signals 新增 fiscal_period 與 (symbol, fiscal_period) 唯一索引（DDP 每財季只推一次）"
# 注意：
# - 只對 v025 建立的 ddp_signals 做 ADD COLUMN；舊資料列的 fiscal_period 為 NULL，
#   SQLite 唯一索引視 NULL 為相異值，因此舊資料不衝突。
sql = """
ALTER TABLE ddp_signals ADD COLUMN fiscal_period TEXT;
CREATE UNIQUE INDEX IF NOT EXISTS idx_ddp_symbol_fiscal_period ON ddp_signals(symbol, fiscal_period);
"""
