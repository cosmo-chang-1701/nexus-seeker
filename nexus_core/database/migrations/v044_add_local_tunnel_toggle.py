version = 44
description = "Add enable_local_tunnel toggle to user_settings"

sql = "ALTER TABLE user_settings ADD COLUMN enable_local_tunnel BOOLEAN DEFAULT 0;"
