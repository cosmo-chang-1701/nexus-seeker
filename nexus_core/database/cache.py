import logging
import json
from datetime import datetime, timedelta, timezone
from typing import Any, Optional
from .financials import get_cached_financials, save_financials_cache, purge_old_cache

from database.connection import get_read_connection, execute_write_async

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


async def purge_stale_kv_cache_dedup_keys(older_than_days: int = 3) -> int:
    """清除 _KV_CACHE_DEDUP_KEY_PREFIXES 白名單前綴下、且 updated_at 早於
    older_than_days 天前的一次性每日去重旗標記錄，避免 kv_cache 無界成長。
    採用 updated_at 而非解析各前綴內嵌的日期字串，因為不同呼叫端內嵌的日期
    格式（YYYY-MM-DD 與 YYYYMMDD）與時區基準（ET 與 UTC）並不一致，統一以
    updated_at 判斷較穩健；預設保留 3 天緩衝，遠超過任何去重旗標實際需要的
    存活時間（僅需存活到當天結束）。回傳成功清除的前綴數量（非精確總筆數，
    因底層 execute_write_async 對 DELETE 的回傳值在 0 筆命中時無法與失敗區分）。
    """
    cutoff_str = (
        datetime.now(timezone.utc) - timedelta(days=older_than_days)
    ).strftime("%Y-%m-%d %H:%M:%S")
    purged_prefixes = 0
    for prefix in _KV_CACHE_DEDUP_KEY_PREFIXES:
        try:
            await execute_write_async(
                "DELETE FROM kv_cache WHERE key LIKE ? AND updated_at < ?",
                (f"{prefix}%", cutoff_str),
            )
            purged_prefixes += 1
        except Exception as e:
            logger.error(
                f"purge_stale_kv_cache_dedup_keys 失敗 (prefix: {prefix}): {e}"
            )
    return purged_prefixes


__all__ = [
    "get_cached_financials",
    "save_financials_cache",
    "purge_old_cache",
    "save_kv_cache",
    "get_kv_cache",
    "purge_stale_kv_cache_dedup_keys",
]
