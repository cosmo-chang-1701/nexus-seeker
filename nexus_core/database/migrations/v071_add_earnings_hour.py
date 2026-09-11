version = 71
description = "Add hour column to earnings_calendar_cache for BMO/AMC earnings timing"
sql = """
ALTER TABLE earnings_calendar_cache ADD COLUMN hour TEXT;
"""
