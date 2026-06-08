version = 46
description = (
    "Add reference_spot_price column to market_cache table for invalidation checking"
)
sql = """
ALTER TABLE market_cache ADD COLUMN reference_spot_price REAL;
"""
