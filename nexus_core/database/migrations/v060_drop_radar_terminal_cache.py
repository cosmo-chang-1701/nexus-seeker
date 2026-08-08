version = 60
description = (
    "Drop redundant radar_terminal_cache table as it is now migrated to kv_cache"
)

sql = """
DROP TABLE IF EXISTS radar_terminal_cache;
"""
