version = 40
description = (
    "Enforce unique constraint on assets table for user_id, symbol, and context_type"
)
sql = """
-- 1. Deduplicate the assets table keeping only the earliest entry for each unique user, symbol, and context_type
DELETE FROM assets
WHERE id NOT IN (
    SELECT MIN(id)
    FROM assets
    GROUP BY user_id, symbol, context_type
);

-- 2. Create unique index to prevent future duplicates
CREATE UNIQUE INDEX IF NOT EXISTS idx_assets_user_symbol_context
ON assets(user_id, symbol, context_type);
"""
