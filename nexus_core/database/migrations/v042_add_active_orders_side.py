version = 42
description = "Add side (BUY/SELL) to active_orders for order direction display"
sql = """
ALTER TABLE active_orders ADD COLUMN side TEXT NOT NULL DEFAULT 'BUY';
"""
