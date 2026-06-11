version = 47
description = (
    "Add consensus_value and fedwatch_probability columns to economic_calendar_events"
)
sql = """
ALTER TABLE economic_calendar_events ADD COLUMN consensus_value TEXT;
ALTER TABLE economic_calendar_events ADD COLUMN fedwatch_probability REAL;
"""
