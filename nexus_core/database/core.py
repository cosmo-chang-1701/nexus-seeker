import sqlite3
import logging
import pkgutil
import importlib
from config import DB_NAME

from database import migrations

logger = logging.getLogger(__name__)

# ==========================================
# 資料庫版本遷移註冊表 (Migration Registry)
# ==========================================
# 每次需要更改資料庫結構時，請在 database/migrations 目錄下新增自立的 python 檔案。
# 系統啟動時會自動掃描該目錄下的所有模組並載入。
def get_migrations():
    migration_list = []
    for _, module_name, _ in pkgutil.iter_modules(migrations.__path__):
        mod = importlib.import_module(f"database.migrations.{module_name}")
        if hasattr(mod, "version") and hasattr(mod, "description") and hasattr(mod, "sql"):
            migration_list.append({
                "version": mod.version,
                "description": mod.description,
                "sql": mod.sql
            })
    migration_list.sort(key=lambda x: x["version"])
    return migration_list

MIGRATIONS = get_migrations()

def run_migrations():
    """執行資料庫版本控管與遷移邏輯"""
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()

    # 1. 確保版控紀錄表存在
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS schema_versions (
            version INTEGER PRIMARY KEY,
            applied_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    ''')

    # 2. 取得目前已套用的最高版本
    cursor.execute('SELECT MAX(version) FROM schema_versions')
    result = cursor.fetchone()[0]
    current_version = result if result is not None else 0

    logger.info(f"目前資料庫 Schema 版本: V{current_version}")

    # 3. 依序執行尚未套用的遷移指令
    for migration in MIGRATIONS:
        v = migration["version"]
        if v > current_version:
            logger.info(f"🚀 正在執行資料庫遷移至 V{v}: {migration['description']}")
            try:
                # 使用 executescript 支援執行多行 SQL 語句
                cursor.executescript(migration["sql"])
                
                # 紀錄該版本已套用
                cursor.execute('INSERT INTO schema_versions (version) VALUES (?)', (v,))
                conn.commit()
                logger.info(f"✅ V{v} 遷移成功！")
            except Exception as e:
                conn.rollback()
                if "duplicate column" in str(e).lower() or "no such column" in str(e).lower():
                    logger.warning(f"⚠️ V{v} 遷移警告: {e} (允許繼續，標記為成功)")
                    cursor.execute('INSERT INTO schema_versions (version) VALUES (?)', (v,))
                    conn.commit()
                else:
                    logger.error(f"❌ V{v} 遷移失敗，已執行 Rollback: {e}")
                    break # 發生 Error 即停止後續遷移，確保資料一致性

    conn.close()

# 為了向下相容，您可以保留 init_db 的名稱，並讓它直接呼叫 run_migrations
def init_db():
    run_migrations()
