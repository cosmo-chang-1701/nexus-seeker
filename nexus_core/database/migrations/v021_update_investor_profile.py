version = 21
description = "Add professional mode fields to user_settings"
sql = '''
ALTER TABLE user_settings ADD COLUMN is_professional_mode BOOLEAN DEFAULT 0;
ALTER TABLE user_settings ADD COLUMN monthly_expense REAL DEFAULT 0.0;
ALTER TABLE user_settings ADD COLUMN tax_reserve_rate REAL DEFAULT 0.20;
'''
