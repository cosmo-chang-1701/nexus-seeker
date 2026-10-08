import logging
import json
import threading
from datetime import datetime, timedelta, timezone
from typing import Any, Mapping, Optional, Sequence
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
    "rollover_alert_",
    "cc_unlock_",
    "price_volume_alert_",
    "wti_alert_",
    "macro_tail_risk_alert_",
    "gamma_squeeze_alert_",
    "advisory_entry_",
    "advisory_exit_",
    "ddp_alert_",
    "iv_alert_",
    "profit_lock_alert_",
    "gamma_fragility_alert_",
    "margin_api_alert_",
    "telemetry_align_",
    "poly_prob_shift_",
    "downside_dd_",
    "downside_cvar_",
    "runway_warn_",
    "runway_remind_",
    "governance_flag_",
)
# 新增任何「每日去重旗標」寫入點時，必須同步加入上方白名單，否則旗標會永久堆積；
# 由 tests/unit/test_kv_cache_dedup_whitelist.py 以 AST 掃描強制。


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


_KV_CACHE_UPSERT_SQL = """
    INSERT INTO kv_cache (key, value, updated_at)
    VALUES (?, ?, CURRENT_TIMESTAMP)
    ON CONFLICT(key) DO UPDATE SET
    value = excluded.value,
    updated_at = CURRENT_TIMESTAMP
"""


async def save_kv_cache_many(items: Mapping[str, Any]) -> bool:
    """一次寫入多個 kv_cache key，整批共用單一交易（全部成功或全部不寫入）。

    供一組彼此相依、必須同時更新的快取鍵使用（例如大盤 GEX 的 spot / flip /
    fallback 旗標 / last-known-good 快照），避免逐鍵呼叫 `save_kv_cache()`
    中途失敗時留下「部分鍵已更新、部分鍵仍為舊值」的不一致狀態。
    """
    if not items:
        return True
    try:
        rows = [(key, json.dumps(value)) for key, value in items.items()]
        await execute_write_many_async([(_KV_CACHE_UPSERT_SQL, rows, True)])
        return True
    except Exception as e:
        logger.error(f"save_kv_cache_many 失敗 (keys: {list(items)}): {e}")
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


def _parse_kv_updated_at(raw: Any) -> Optional[datetime]:
    """kv_cache.updated_at（SQLite CURRENT_TIMESTAMP，UTC）解析為 aware UTC datetime。

    解析失敗回傳 None（呼叫端視為年齡未知，而非誤判為新鮮）。所有 kv 時間運算
    一律在 UTC 上進行，需要美東日期時才在最後一步轉 ET。
    """
    try:
        return datetime.strptime(raw, "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
    except Exception:
        return None


def get_kv_cache_with_updated_at(
    key: str,
) -> tuple[Optional[Any], Optional[datetime]]:
    """同 get_kv_cache()，但額外回傳 `updated_at`（aware UTC datetime）。

    供需要「寫入時間本身」的判斷使用（例如以交易日為準的新鮮度）：直接用
    updated_at 比「now − age」反推可靠。查無資料回傳 `(None, None)`；
    updated_at 解析失敗時為 `(value, None)`。
    """
    conn = None
    try:
        conn = get_read_connection()
        cursor = conn.cursor()
        cursor.execute("SELECT value, updated_at FROM kv_cache WHERE key = ?", (key,))
        row = cursor.fetchone()
        if row:
            return json.loads(row[0]), _parse_kv_updated_at(row[1])
    except Exception as e:
        logger.error(f"get_kv_cache_with_updated_at 失敗 (key: {key}): {e}")
    finally:
        if conn:
            conn.close()
    return None, None


def get_kv_cache_with_age(key: str) -> tuple[Optional[Any], Optional[float]]:
    """同 get_kv_cache()，但額外回傳資料年齡（秒），供新鮮度標示與時間戳顯示共用。
    查無資料或 updated_at 解析失敗時，年齡回傳 None（而非誤判為新鮮）。"""
    value, updated_dt = get_kv_cache_with_updated_at(key)
    if updated_dt is None:
        return value, None
    return value, (datetime.now(timezone.utc) - updated_dt).total_seconds()


def get_kv_cache_fresh(key: str, max_age_seconds: float) -> Optional[Any]:
    """讀取 kv_cache，僅在資料年齡 <= max_age_seconds 時回傳值，否則回傳 None。

    與 get_kv_cache_with_age() 的關係：本函式是其薄封裝，把「讀值＋檢查年齡」
    收斂成單一呼叫，供各 fallback 讀取點套用年齡上限。年齡未知 (None，例如
    updated_at 解析失敗) 一律視為不可用，避免把來源不明的舊值誤當新鮮資料。
    查無資料、逾期、年齡未知皆回傳 None，由呼叫端走既有的缺值路徑。
    """
    value, age_seconds = get_kv_cache_with_age(key)
    if value is None or age_seconds is None or age_seconds > max_age_seconds:
        return None
    return value


def session_fresh_value(
    value: Any,
    updated_at: Optional[datetime],
    as_of: Optional[datetime] = None,
) -> Any:
    """`(value, updated_at)` 的交易日新鮮度判斷：寫入於最近已收盤交易日當天或之後才回傳值。

    `get_kv_cache_session_fresh`（單 key 讀取）與雷達 `_KvSnapshot.get_session_fresh`
    （批次快照）共用此單一實作，避免兩處各自維護同一規則。查無值、逾期、
    updated_at 未知皆回傳 None。
    """
    import market_time

    if value is None or not market_time.is_updated_within_last_session(
        updated_at, as_of
    ):
        return None
    return value


def get_kv_cache_session_fresh(key: str, as_of: Optional[datetime] = None) -> Any:
    """讀取 kv_cache，僅在「寫入於最近一個已收盤交易日當天或之後」時回傳值。

    以交易日而非固定小時數判斷（見 `market_time.is_updated_within_last_session`），
    供 Vol POC／GEX PutWall 這類價位回退使用，避免週末／連假讓備援全數失效。
    查無資料、逾期、年齡未知皆回傳 None。`as_of` 只決定「上一個已收盤交易日」
    （預設現在），寫入日期一律取自 kv 的 updated_at。
    """
    value, updated_at = get_kv_cache_with_updated_at(key)
    return session_fresh_value(value, updated_at, as_of)


# FedWatch 鷹派傾向分數（kv `macro_fedwatch_probability`）供下游讀取的最大年齡。
# 寫入端為 4 小時週期的宏觀／FedWatch 檢查，12 小時 = 寫入週期 × 3，容許連續兩次
# 刷新失敗仍可用；超過則視為過期（進場閘門 prob=None，評分函式不計分、不放行 NORMAL）。
# 定義於 database 層，避免 market_analysis 為取常數反向依賴 services。
FEDWATCH_PROB_MAX_AGE_SECONDS: float = 12 * 3600.0
# 顯示字串與 log 用的小時數，由上限推導，避免「逾 12 小時」寫死而與常數漂移。
FEDWATCH_PROB_MAX_AGE_HOURS: int = int(FEDWATCH_PROB_MAX_AGE_SECONDS // 3600)


def get_fedwatch_probability_last_known() -> tuple[Optional[Any], bool]:
    """讀取 FedWatch 分數的「最後一筆已知值」，回傳 `(prob, is_stale)`，不因逾期丟棄。

    供避險端（保護性 Put 逃頂防禦）使用：避險寧可沿用舊值計分，也不要因資料過期
    而少算一個風險因子（fail-safe for hedging）。進場閘門請改用
    `get_fedwatch_probability_fresh()`（fail-closed）。

    - 有值且年齡 <= FEDWATCH_PROB_MAX_AGE_SECONDS：`(prob, False)`。
    - 有值但逾期或年齡未知：`(prob, True)`。
    - 查無資料：`(None, False)`（單純無資料，不是過期）。
    """
    value, age_seconds = get_kv_cache_with_age("macro_fedwatch_probability")
    if value is None:
        return None, False
    if age_seconds is None or age_seconds > FEDWATCH_PROB_MAX_AGE_SECONDS:
        return value, True
    return value, False


def get_fedwatch_probability_fresh() -> tuple[Optional[Any], bool]:
    """讀取 FedWatch 分數，回傳 `(prob, is_stale)`（進場閘門／簡報用，fail-closed）。

    - 有值且年齡 <= FEDWATCH_PROB_MAX_AGE_SECONDS：`(prob, False)`。
    - 有值但逾期或年齡未知：`(None, True)`，呼叫端據此標示「資料過期」。
    - 查無資料：`(None, False)`（單純無資料，不是過期）。
    """
    value, is_stale = get_fedwatch_probability_last_known()
    if is_stale:
        return None, True
    return value, False


# 「每段過期期間只警告一次」的狀態：scope → 該 scope 是否已對目前這段過期警告過。
# 各呼叫端（進場閘門、避險、簡報）每 15 分鐘～數分鐘讀一次，過期期間若每次都
# logger.warning 會洗版；同一筆過期資料警告一次即可，資料恢復新鮮後重置，
# 下次再過期會再警告一次。
_fedwatch_stale_warned: dict[str, bool] = {}
_fedwatch_stale_warn_lock = threading.Lock()


def should_warn_fedwatch_stale(scope: str, is_stale: bool) -> bool:
    """FedWatch 過期警告節流：該 scope 在這段過期期間第一次回傳 True，其餘 False。

    每次讀取都要傳入當下的 `is_stale`：`is_stale=False`（資料恢復新鮮）會重置
    該 scope 的旗標。`scope` 為呼叫端識別字串（如 "vetoes"、"defense"）。
    """
    with _fedwatch_stale_warn_lock:
        if not is_stale:
            _fedwatch_stale_warned.pop(scope, None)
            return False
        if _fedwatch_stale_warned.get(scope):
            return False
        _fedwatch_stale_warned[scope] = True
        return True


def get_kv_cache_many_with_updated_at(
    keys: Sequence[str],
) -> dict[str, tuple[Any, Optional[datetime]]]:
    """一次讀取多個 kv_cache key（單一連線、單一查詢），回傳 `{key: (value, updated_at)}`。

    `updated_at` 為 aware UTC datetime（解析失敗為 None）。查無資料的 key 不會出現
    在結果中。`get_kv_cache_many` 以此為底，只是把 updated_at 換算為年齡。

    存在理由：熱路徑（雷達掃描）原本逐 key 呼叫 `get_kv_cache()`，每次都
    connect-query-close 一條新連線；單一標的就是十幾條連線，再乘上整個 watchlist
    的 `asyncio.gather`，等於每輪心跳在 event loop 上連開數百條連線。
    """
    if not keys:
        return {}

    unique_keys = list(dict.fromkeys(keys))
    results: dict[str, tuple[Any, Optional[datetime]]] = {}
    conn = None
    try:
        conn = get_read_connection()
        cursor = conn.cursor()
        placeholders = ",".join("?" for _ in unique_keys)
        # nosemgrep: python.lang.security.audit.formatted-sql-query.formatted-sql-query, python.sqlalchemy.security.sqlalchemy-execute-raw-query.sqlalchemy-execute-raw-query
        cursor.execute(
            f"SELECT key, value, updated_at FROM kv_cache WHERE key IN ({placeholders})",
            tuple(unique_keys),
        )
        for key, raw_value, updated_at in cursor.fetchall():
            try:
                value = json.loads(raw_value)
            except Exception:
                continue
            results[key] = (value, _parse_kv_updated_at(updated_at))
    except Exception as e:
        logger.error(f"get_kv_cache_many 失敗 ({len(unique_keys)} keys): {e}")
    finally:
        if conn:
            conn.close()
    return results


def get_kv_cache_many(
    keys: Sequence[str],
) -> dict[str, tuple[Any, Optional[float]]]:
    """一次讀取多個 kv_cache key（單一連線、單一查詢）。

    回傳 `{key: (value, age_seconds)}`，查無資料的 key 不會出現在結果中。
    """
    now = datetime.now(timezone.utc)
    return {
        key: (
            value,
            (now - updated_at).total_seconds() if updated_at is not None else None,
        )
        for key, (value, updated_at) in get_kv_cache_many_with_updated_at(keys).items()
    }


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


async def purge_stale_kv_cache_by_prefix(
    prefixes: tuple[str, ...], older_than_days: int
) -> int:
    """清除指定前綴下、updated_at 早於 older_than_days 天前的 kv_cache 列。

    與 `purge_stale_kv_cache_dedup_keys` 不同：這裡處理的是「具快取語意、但標的
    下市／移出清單後永不再被讀寫」的殘留列（如 company_profile_／etf_flag_），
    不是每日去重旗標，故前綴由呼叫端傳入，不屬於 `_KV_CACHE_DEDUP_KEY_PREFIXES`。
    以 GLOB（大小寫敏感、可走主鍵索引）參數化比對；整批併為單一交易。
    回傳實際清除的資料列總數；失敗回傳 0。
    """
    if not prefixes:
        return 0
    cutoff_str = (
        datetime.now(timezone.utc) - timedelta(days=older_than_days)
    ).strftime("%Y-%m-%d %H:%M:%S")
    statements = [
        (
            "DELETE FROM kv_cache WHERE key GLOB ? AND updated_at < ?",
            (f"{prefix}*", cutoff_str),
        )
        for prefix in prefixes
    ]
    try:
        rowcounts = await execute_write_many_async(statements)
        return sum(max(0, n) for n in rowcounts)
    except Exception as e:
        logger.error(f"purge_stale_kv_cache_by_prefix 失敗: {e}")
        return 0


__all__ = [
    "get_cached_financials",
    "save_financials_cache",
    "purge_old_cache",
    "save_kv_cache",
    "save_kv_cache_many",
    "get_kv_cache",
    "get_kv_cache_with_age",
    "get_kv_cache_with_updated_at",
    "get_kv_cache_session_fresh",
    "session_fresh_value",
    "get_fedwatch_probability_fresh",
    "get_fedwatch_probability_last_known",
    "should_warn_fedwatch_stale",
    "FEDWATCH_PROB_MAX_AGE_SECONDS",
    "FEDWATCH_PROB_MAX_AGE_HOURS",
    "get_kv_cache_many",
    "get_kv_cache_many_with_updated_at",
    "purge_stale_kv_cache_dedup_keys",
    "purge_stale_kv_cache_by_prefix",
]
