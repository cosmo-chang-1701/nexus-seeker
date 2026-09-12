from typing import Any
import sqlite3
from database.migrations import v072_remove_margin_buying_power as mig_v072
from database.user_settings import (
    UserContext,
    upsert_user_config,
    get_full_user_context,
)


def test_v072_migration_metadata() -> None:
    """驗證 v072 遷移模組符合 database/core.py 要求的 version, description, sql 契約。"""
    assert mig_v072.version == 72
    assert isinstance(mig_v072.description, str)
    assert len(mig_v072.description) > 0
    assert "DROP COLUMN option_buying_power" in mig_v072.sql
    assert "DROP COLUMN margin_used" in mig_v072.sql


def test_user_settings_schema_has_no_margin_buying_power(db_conn: Any) -> None:
    """驗證資料庫 user_settings 資料表中已無 option_buying_power 與 margin_used 欄位。"""
    cursor = db_conn.cursor()
    cursor.execute("PRAGMA table_info(user_settings)")
    columns = [row[1] for row in cursor.fetchall()]
    assert "option_buying_power" not in columns
    assert "margin_used" not in columns


def test_user_context_has_no_margin_buying_power_attributes() -> None:
    """驗證 UserContext dataclass 已移除 option_buying_power 與 margin_used 屬性。"""
    ctx = UserContext(
        user_id=123456,
        capital=100000.0,
        risk_limit=15.0,
        total_weighted_delta=0.0,
        total_theta=0.0,
        total_gamma=0.0,
    )
    assert not hasattr(ctx, "option_buying_power")
    assert not hasattr(ctx, "margin_used")
    # 確保核心風控指標保留
    assert hasattr(ctx, "capital")
    assert hasattr(ctx, "cash_reserve")
    assert hasattr(ctx, "risk_limit")


def test_upsert_user_config_ignores_deprecated_keys(db_conn: Any) -> None:
    """驗證 upsert_user_config 拒絕更新已廢棄的 option_buying_power 與 margin_used。"""
    user_id = 999888
    # 建立使用者記錄
    upsert_user_config(user_id, monthly_expense=5000.0)

    # 嘗試只更新廢棄欄位 -> 應回傳 False (無任何欄位被更新)
    res = upsert_user_config(
        user_id,
        option_buying_power=50000.0,
        margin_used=20000.0,
    )
    assert res is False

    # 混合更新：有效欄位與廢棄欄位同時傳入 -> 應只更新有效欄位，忽略廢棄欄位
    res_mixed = upsert_user_config(
        user_id,
        monthly_expense=6500.0,
        option_buying_power=50000.0,
    )
    assert res_mixed is True

    ctx = get_full_user_context(user_id)
    assert not hasattr(ctx, "option_buying_power")
    assert not hasattr(ctx, "margin_used")
    assert ctx.monthly_expense == 6500.0


def test_v072_migration_live_upgrade_preserves_data() -> None:
    """驗證從含 option_buying_power / margin_used 的舊版本 schema 升級至 v072 時，
    舊欄位成功刪除且既有使用者之核心風控與財務數據 (capital, cash_reserve 等) 完好無損。"""
    conn = sqlite3.connect(":memory:")
    cursor = conn.cursor()
    # 模擬 v071 或更早版本：user_settings 包含 option_buying_power 與 margin_used
    cursor.execute("""
        CREATE TABLE user_settings (
            user_id INTEGER PRIMARY KEY,
            capital REAL DEFAULT 100000.0,
            cash_reserve REAL DEFAULT 0.0,
            monthly_expense REAL DEFAULT 3000.0,
            option_buying_power REAL DEFAULT 0.0,
            margin_used REAL DEFAULT 0.0
        );
    """)
    cursor.execute("""
        INSERT INTO user_settings (user_id, capital, cash_reserve, monthly_expense, option_buying_power, margin_used)
        VALUES (888999, 250000.0, 50000.0, 4500.0, 75000.0, 30000.0);
    """)
    conn.commit()

    # 執行 v072 遷移指令
    cursor.executescript(mig_v072.sql)
    conn.commit()

    # 驗證結構：廢棄欄位已從 schema 移除
    cursor.execute("PRAGMA table_info(user_settings)")
    cols = [row[1] for row in cursor.fetchall()]
    assert "option_buying_power" not in cols
    assert "margin_used" not in cols
    assert "capital" in cols
    assert "cash_reserve" in cols
    assert "monthly_expense" in cols

    # 驗證數據：既有使用者核心資料完好保留，無損毀
    cursor.execute(
        "SELECT user_id, capital, cash_reserve, monthly_expense FROM user_settings WHERE user_id = 888999"
    )
    row = cursor.fetchone()
    assert row == (888999, 250000.0, 50000.0, 4500.0)
    conn.close()
