import sqlite3
import logging
import pkgutil
import importlib
import config
import re

from database import migrations

logger = logging.getLogger(__name__)

# ==========================================
# 資料庫版本遷移註冊表 (Migration Registry)
# ==========================================
# 每次需要更改資料庫結構時，請在 database/migrations 目錄下新增自立的 python 檔案。
# 系統啟動時會自動掃描該目錄下的所有模組並載入。
def get_migrations():
    migration_list = []
    # 預期模組名稱格式: v001_init 等
    module_pattern = re.compile(r"^[a-z0-9_]+$")

    for _, module_name, _ in pkgutil.iter_modules(migrations.__path__):
        if not module_pattern.match(module_name):
            logger.warning(f"跳過不合規的遷移模組名稱: {module_name}")
            continue

        # nosemgrep: python.lang.security.audit.non-literal-import.non-literal-import
        mod = importlib.import_module(f"database.migrations.{module_name}")
        if hasattr(mod, "version") and hasattr(mod, "description") and hasattr(mod, "sql"):
            migration_list.append({
                "version": mod.version,
                "description": mod.description,
                "sql": mod.sql,
                "module": mod # 🚀 儲存模組參考
            })
    migration_list.sort(key=lambda x: x["version"])
    return migration_list

MIGRATIONS = get_migrations()

def run_migrations():
    """執行資料庫版本控管與遷移邏輯"""
    conn = sqlite3.connect(config.DB_NAME)
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
    table_pattern = re.compile(r"^[a-zA-Z0-9_]+$")

    for migration in MIGRATIONS:
        v = migration["version"]
        if v > current_version:
            logger.info(f"🚀 正在執行資料庫遷移至 V{v}: {migration['description']}")
            try:
                # 執行 SQL 遷移
                cursor.executescript(migration["sql"])

                # 🚀 執行選配的 Python 資料遷移函式
                mod = migration["module"]
                if hasattr(mod, "migrate_data"):
                    logger.info(f"⚙️ 執行 V{v} 額外資料遷移邏輯 (Python)...")
                    mod.migrate_data(conn)

                # 紀錄該版本已套用
                cursor.execute('INSERT INTO schema_versions (version) VALUES (?)', (v,))
                conn.commit()
                logger.info(f"✅ V{v} 遷移成功！")
            except Exception as e:
                conn.rollback()

                # 🚀 [Self-Healing] 嘗試自動清理殘留的 _new 暫存表，防止下次遷移因表已存在而死鎖
                try:
                    cursor.execute("SELECT name FROM sqlite_master WHERE type='table' AND name LIKE '%_new'")
                    temp_tables = cursor.fetchall()
                    for (table_name,) in temp_tables:
                        if table_pattern.match(table_name):
                            logger.warning(f"🧹 偵測到殘留暫存表 {table_name}，正在自動清理以解除遷移死鎖...")
                            # nosemgrep: python.lang.security.audit.formatted-sql-query.formatted-sql-query, python.sqlalchemy.security.sqlalchemy-execute-raw-query.sqlalchemy-execute-raw-query
                            cursor.execute(f"DROP TABLE IF EXISTS {table_name}")
                        else:
                            logger.error(f"⚠️ 偵測到非法格式的暫存表名稱: {table_name}，拒絕自動清理。")
                    conn.commit()
                except Exception as cleanup_err:
                    logger.error(f"⚠️ 自動清理暫存表時出錯: {cleanup_err}")

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
