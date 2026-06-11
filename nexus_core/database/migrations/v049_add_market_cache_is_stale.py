version = 49
description = "Add is_stale column to market_cache table for SWR tracking"
sql = """
ALTER TABLE market_cache ADD COLUMN is_stale INTEGER DEFAULT 0;
"""
