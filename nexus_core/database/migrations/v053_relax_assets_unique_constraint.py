version = 53
description = "Enforce unique constraint on assets table only for non-TRADE contexts"
sql = """
-- 1. Drop existing unique index
DROP INDEX IF EXISTS idx_assets_user_symbol_context;

-- 2. Re-create unique index excluding 'TRADE' context_type
CREATE UNIQUE INDEX IF NOT EXISTS idx_assets_user_symbol_context
ON assets(user_id, symbol, context_type)
WHERE context_type != 'TRADE';
"""
