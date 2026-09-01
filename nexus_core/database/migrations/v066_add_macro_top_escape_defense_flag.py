version = 66
description = "新增 enable_macro_top_escape_defense 欄位，作為宏觀逃頂前瞻防禦 (Dynamic Rollover Scenario 6) 選擇加入開關"
sql = """
ALTER TABLE user_settings ADD COLUMN enable_macro_top_escape_defense BOOLEAN DEFAULT 0;
"""
