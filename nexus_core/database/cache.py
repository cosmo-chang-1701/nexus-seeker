import logging
import json
from datetime import datetime, timedelta, timezone
from typing import Any, Optional, Sequence
from .financials import get_cached_financials, save_financials_cache, purge_old_cache

from database.connection import (
    get_read_connection,
    execute_write_async,
    execute_write_many_async,
)

logger = logging.getLogger(__name__)

# 這些前綴皆為「單次派發防重複」用途的每日去重旗標（例如今天是否已對某使用者
# 發過某標的的某類告警），寫入後只會被 get_kv_cache 檢查是否存在，值本身
# （恆為 1/True）永遠不會被讀取消費。一旦當天過去，這些 row 就再無任何用途，
# 但 kv_cache 沒有 TTL 欄位、也沒有排程清理，過去會隨時間無限累積。
# 刻意採用白名單前綴（而非依 updated_at 全域清除），避免誤刪任何具持久意義的
# 快取（如 last-known-good 備援快照、使用者設定、月度/年度資料）。
_KV_CACHE_DEDUP_KEY_PREFIXES: tuple[str, ...] = (
    "scenario_alert_",
    "rollover_alert_",
    "cc_unlock_",
    "price_volume_alert_",
    "wti_alert_",
    "macro_tail_risk_alert_",
    "gamma_squeeze_alert_",
)


async def save_kv_cache(key: str, value: Any) -> bool:
    try:
        val_str = json.dumps(value)
        await execute_write_async(
            """
            INSERT INTO kv_cache (key, value, updated_at)
            VALUES (?, ?, CURRENT_TIMESTAMP)
            ON CONFLICT(key) DO UPDATE SET
            value = excluded.value,
            updated_at = CURRENT_TIMESTAMP
        """,
            (key, val_str),
        )
        return True
    except Exception as e:
        logger.error(f"save_kv_cache 失敗 (key: {key}): {e}")
        return False


def get_kv_cache(key: str) -> Optional[Any]:
    conn = None
    try:
        conn = get_read_connection()
        cursor = conn.cursor()
        cursor.execute("SELECT value FROM kv_cache WHERE key = ?", (key,))
        row = cursor.fetchone()
        if row:
            return json.loads(row[0])
    except Exception as e:
        logger.error(f"get_kv_cache 失敗 (key: {key}): {e}")
    finally:
        if conn:
            conn.close()
    return None


def get_kv_cache_with_age(key: str) -> tuple[Optional[Any], Optional[float]]:
    """同 get_kv_cache()，但額外回傳資料年齡（秒），供新鮮度標示與時間戳顯示共用。
    查無資料或 updated_at 解析失敗時，年齡回傳 None（而非誤判為新鮮）。"""
    conn = None
    try:
        conn = get_read_connection()
        cursor = conn.cursor()
        cursor.execute("SELECT value, updated_at FROM kv_cache WHERE key = ?", (key,))
        row = cursor.fetchone()
        if row:
            value = json.loads(row[0])
            age_seconds: Optional[float] = None
            try:
                updated_dt = datetime.strptime(row[1], "%Y-%m-%d %H:%M:%S").replace(
                    tzinfo=timezone.utc
                )
                age_seconds = (datetime.now(timezone.utc) - updated_dt).total_seconds()
            except Exception:
                pass
            return value, age_seconds
    except Exception as e:
        logger.error(f"get_kv_cache_with_age 失敗 (key: {key}): {e}")
    finally:
        if conn:
            conn.close()
    return None, None


def get_kv_cache_many(
    keys: Sequence[str],
) -> dict[str, tuple[Any, Optional[float]]]:
    """一次讀取多個 kv_cache key（單一連線、單一查詢）。

    回傳 `{key: (value, age_seconds)}`，查無資料的 key 不會出現在結果中。

    存在理由：熱路徑（雷達掃描）原本逐 key 呼叫 `get_kv_cache()`，每次都
    connect-query-close 一條新連線；單一標的就是十幾條連線，再乘上整個 watchlist
    的 `asyncio.gather`，等於每輪心跳在 event loop 上連開數百條連線。
    """
    if not keys:
        return {}

    unique_keys = list(dict.fromkeys(keys))
    results: dict[str, tuple[Any, Optional[float]]] = {}
    conn = None
    try:
        conn = get_read_connection()
        cursor = conn.cursor()
        placeholders = ",".join("?" for _ in unique_keys)
        # nosemgrep: python.lang.security.audit.formatted-sql-query.formatted-sql-query
        cursor.execute(
            f"SELECT key, value, updated_at FROM kv_cache WHERE key IN ({placeholders})",
            tuple(unique_keys),
        )
        now = datetime.now(timezone.utc)
        for key, raw_value, updated_at in cursor.fetchall():
            try:
                value = json.loads(raw_value)
            except Exception:
                continue
            age_seconds: Optional[float] = None
            try:
                updated_dt = datetime.strptime(updated_at, "%Y-%m-%d %H:%M:%S").replace(
                    tzinfo=timezone.utc
                )
                age_seconds = (now - updated_dt).total_seconds()
            except Exception:
                pass
            results[key] = (value, age_seconds)
    except Exception as e:
        logger.error(f"get_kv_cache_many 失敗 ({len(unique_keys)} keys): {e}")
    finally:
        if conn:
            conn.close()
    return results


async def purge_stale_kv_cache_dedup_keys(older_than_days: int = 3) -> int:
    """清除 _KV_CACHE_DEDUP_KEY_PREFIXES 白名單前綴下、且 updated_at 早於
    older_than_days 天前的一次性每日去重旗標記錄，避免 kv_cache 無界成長。
    採用 updated_at 而非解析各前綴內嵌的日期字串，因為不同呼叫端內嵌的日期
    格式（YYYY-MM-DD 與 YYYYMMDD）與時區基準（ET 與 UTC）並不一致，統一以
    updated_at 判斷較穩健；預設保留 3 天緩衝，遠超過任何去重旗標實際需要的
    存活時間（僅需存活到當天結束）。回傳實際清除的**資料列總數**。

    （早期版本只能回傳「嘗試過的前綴數量」，因為 execute_write_async 對 DELETE
    的回傳值在命中 0 筆時無法與失敗區分；批次寫入入口現在會回傳逐語句的
    rowcount，因此可以給出精確筆數。）
    """
    cutoff_str = (
        datetime.now(timezone.utc) - timedelta(days=older_than_days)
    ).strftime("%Y-%m-%d %H:%M:%S")

    # 以 GLOB 取代 LIKE：SQLite 的 LIKE 預設對 ASCII 大小寫不敏感，因此
    # `key LIKE 'prefix%'` 無法利用 key 的主鍵索引，每個前綴都是一次全表掃描
    # （而且過去是 7 次各自獨立的交易）。GLOB 大小寫敏感，前綴樣式可以走索引。
    # 這些鍵全部由程式碼以固定大小寫組出，改用 GLOB 不會改變比對結果。
    # 整批併為單一交易，7 次 commit 降為 1 次。
    statements = [
        (
            "DELETE FROM kv_cache WHERE key GLOB ? AND updated_at < ?",
            (f"{prefix}*", cutoff_str),
        )
        for prefix in _KV_CACHE_DEDUP_KEY_PREFIXES
    ]
    try:
        rowcounts = await execute_write_many_async(statements)
        return sum(max(0, n) for n in rowcounts)
    except Exception as e:
        logger.error(f"purge_stale_kv_cache_dedup_keys 失敗: {e}")
        return 0


__all__ = [
    "get_cached_financials",
    "save_financials_cache",
    "purge_old_cache",
    "save_kv_cache",
    "get_kv_cache",
    "get_kv_cache_with_age",
    "get_kv_cache_many",
    "purge_stale_kv_cache_dedup_keys",
]
