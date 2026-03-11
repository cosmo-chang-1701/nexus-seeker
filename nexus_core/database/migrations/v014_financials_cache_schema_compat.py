version = 14
description = "Ensure financials_cache supports data payload column and updated_at index"
sql = """
ALTER TABLE financials_cache ADD COLUMN data TEXT;
UPDATE financials_cache SET data = metrics WHERE data IS NULL;
CREATE INDEX IF NOT EXISTS idx_financials_updated
ON financials_cache(updated_at);
"""
