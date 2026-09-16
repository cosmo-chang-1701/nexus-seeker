from typing import Any

version = 74
description = (
    "新增 market_cache.put_wall 與 market_cache.previous_put_wall 欄位以追蹤做市商"
    "支撐底牆跨週期遷移（做空部位 TP2-空間擴展的向下遷移判定所需，鏡像 v069）"
)
sql = ""


def migrate_data(conn: Any) -> None:
    """完整鏡像 v069_add_previous_call_wall 的作法。

    做空部位的 TP2-空間擴展需要判定「做市商支撐牆向下遷移 >= 3%」，這是多頭
    TP2「阻力牆向上遷移」的鏡像。v069 只加了 call_wall / previous_call_wall
    兩欄，缺少對應的 put_wall 側，導致做空 TP2 的遷移分支永遠無法觸發（只會
    退回 1.5% 跌破判定）。本遷移補齊該資料通路。

    逐欄 ALTER 並各自吞掉 duplicate column 例外：SQLite 的 ALTER TABLE 不支援
    IF NOT EXISTS，且 database/core.py 的遷移執行器一旦拋例外就會中斷整批，
    故沿用 v069 的逐欄 try/except 模式以保證重複執行時的冪等性。
    """
    cursor = conn.cursor()
    for statement in (
        "ALTER TABLE market_cache ADD COLUMN put_wall REAL DEFAULT NULL",
        "ALTER TABLE market_cache ADD COLUMN previous_put_wall REAL DEFAULT NULL",
    ):
        try:
            cursor.execute(statement)
        except Exception as e:
            if (
                "duplicate column name" in str(e).lower()
                or "already exists" in str(e).lower()
            ):
                continue
            raise e
