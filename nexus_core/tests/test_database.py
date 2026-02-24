import unittest
import sqlite3
import os
from database.core import MIGRATIONS, run_migrations
from config import DB_NAME

class TestDatabaseMigrations(unittest.TestCase):
    def setUp(self):
        # Use in-memory SQLite for testing or a test db file
        # config.DB_NAME is already used by the core.py, we might want to override it
        if os.path.exists(DB_NAME):
            os.remove(DB_NAME)
            
    def tearDown(self):
        if os.path.exists(DB_NAME):
            os.remove(DB_NAME)

    def test_migrations_loaded(self):
        self.assertGreaterEqual(len(MIGRATIONS), 3)
        self.assertEqual(MIGRATIONS[0]["version"], 1)
        self.assertEqual(MIGRATIONS[1]["version"], 2)
        self.assertEqual(MIGRATIONS[2]["version"], 3)
        
    def test_run_migrations(self):
        run_migrations()
        # Verify schema
        conn = sqlite3.connect(DB_NAME)
        cursor = conn.cursor()
        
        # Check versions table
        cursor.execute("SELECT MAX(version) FROM schema_versions")
        max_version = cursor.fetchone()[0]
        self.assertGreaterEqual(max_version, 3)
        
        # Check portfolio schema
        cursor.execute("PRAGMA table_info(portfolio)")
        columns = [col[1] for col in cursor.fetchall()]
        self.assertIn("user_id", columns)
        self.assertIn("stock_cost", columns)
        self.assertNotIn("is_covered", columns)
        
        conn.close()

if __name__ == '__main__':
    unittest.main()
