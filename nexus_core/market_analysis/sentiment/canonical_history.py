"""canonical_history.py — 情緒指標的日級規範母體 (`sentiment_daily_canonical`)。

為什麼存在：`sentiment_history` 是高頻日誌。`calculate_skew()` 有十多個呼叫端
（15 分鐘心跳、/x、終端、Analyst runner、委託單遙測…），每次呼叫都寫一筆，
取樣頻率不固定。拿「最近 500 列」當百分位母體，實際只涵蓋兩三週，而且相鄰
樣本高度自相關：安靜期的微小跳動就能衝上高分位，連續數週的恐慌反而被當成常態。

本模組把每個交易日重採樣成恰好一筆（當日**最後一筆盤中觀測**），百分位改以
最近 252 個交易日為母體。

設計約束：
- **重採樣只有一個定義** `resample_daily_close()`，v080 migration 回填、16:15 ET
  收盤快照與 08:45 ET 補寫三處共用。
- **不做任何網路抓取**。收盤後 yfinance 期權鏈的 bid/ask 常歸零、IV 失真，
  盤後重抓的「收盤值」不可靠；當日最後一筆盤中觀測才是可信的收盤代理。
  以 NYSE 行事曆的 `market_close` 為界，半日市（13:00 ET）自然正確。
- **寫入冪等** (`INSERT OR IGNORE`)：已寫入的交易日不會被重跑覆寫。
- 舊 `"SKEW"` 序列是 ±5% 履約價代理值，與 25-Delta 定義不同（見
  `skew_taxonomy.py`），**不得**映射進 `SKEW_D25`。
"""

import logging
import math
import statistics
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Iterable, Mapping, Optional, Sequence
from zoneinfo import ZoneInfo

from .skew_taxonomy import SKEW_INDICATOR

logger = logging.getLogger(__name__)

_NY_TZ = ZoneInfo("America/New_York")
_TS_FMT = "%Y-%m-%d %H:%M:%S"

# 進入日級母體的指標。IV 已有自己的日級表 (`historical_iv`)，不重複收錄。
CANONICAL_INDICATORS: tuple[str, ...] = (SKEW_INDICATOR, "PCR")

# 百分位母體視窗：一年的交易日數。
CANONICAL_WINDOW_DAYS = 252
# 低於此樣本數時不使用規範母體，呼叫端退回既有的高頻池（行為與改版前相同）。
CANONICAL_MIN_SAMPLES = 20
# 達到此樣本數才標記為成熟母體 (`is_canonical=True`)。
CANONICAL_MATURE_SAMPLES = 60
# 保留期：多留幾天的緩衝，避免清理與視窗邊界剛好重疊。
CANONICAL_RETENTION_TRADING_DAYS = CANONICAL_WINDOW_DAYS + 8

# Robust Z 的 IQR 下限（單位與指標值相同：Skew 為百分點、PCR 為比值）。
# IQR 低於此值代表母體幾乎沒有離散度（常見於資料源卡住、回傳同一組 IV），
# 此時 Z 分數的分母趨近 0，任何微小跳動都會被放大成天文數字，因此改回傳 None。
# 數值為 PRE_CALIBRATION 保守值，調整需走 calibration/ 報告流程。
ROBUST_Z_MIN_IQR: dict[str, float] = {SKEW_INDICATOR: 0.25, "PCR": 0.05}
# IQR / 1.349 為常態分佈下標準差的穩健估計。
_IQR_TO_SIGMA = 1.349


@dataclass(frozen=True)
class CanonicalStats:
    """單一指標相對於規範母體的統計結果。"""

    percentile: Optional[float]
    sample_size: int
    robust_z: Optional[float]
    is_canonical: bool


def _parse_utc(ts: Any) -> Optional[datetime]:
    """把 SQLite `CURRENT_TIMESTAMP` (UTC、無時區) 或 ISO 字串解析為 aware UTC。"""
    if not ts:
        return None
    try:
        dt = datetime.fromisoformat(str(ts))
    except ValueError:
        return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def resample_daily_close(
    rows: Iterable[Sequence[Any]],
    sessions: Mapping[str, tuple[str, str]],
    indicators: Sequence[str] = CANONICAL_INDICATORS,
) -> list[tuple[str, str, str, float]]:
    """把高頻觀測重採樣為「每標的 × 交易日 × 指標」一筆收盤代理值。

    `rows` 為 `(symbol, indicator, value, timestamp_utc)`，須依寫入順序排列
    （同一秒的兩筆以後寫入者為準）。`sessions` 為
    `market_time.get_session_bounds_utc()` 的回傳值。

    規則：只收交易日盤中 `[market_open, market_close]` 的觀測，取時間最晚的一筆。
    盤前與盤後的觀測期權鏈報價都已凍結，週末的 on-demand 查詢也一樣，一律丟棄。

    回傳 `(SYMBOL, trade_date, indicator, value)` 清單。
    """
    wanted = set(indicators)
    best: dict[tuple[str, str, str], tuple[str, float]] = {}
    for row in rows:
        try:
            symbol, indicator, value, ts = row[0], row[1], row[2], row[3]
        except (IndexError, TypeError):
            continue
        if not symbol or indicator not in wanted or value is None:
            continue
        try:
            fval = float(value)
        except (TypeError, ValueError):
            continue
        if not math.isfinite(fval):
            continue
        dt_utc = _parse_utc(ts)
        if dt_utc is None:
            continue
        trade_date = dt_utc.astimezone(_NY_TZ).strftime("%Y-%m-%d")
        bounds = sessions.get(trade_date)
        if bounds is None:
            continue
        ts_norm = dt_utc.strftime(_TS_FMT)
        if not (bounds[0] <= ts_norm <= bounds[1]):
            continue
        key = (str(symbol).upper(), trade_date, str(indicator))
        prev = best.get(key)
        if prev is None or ts_norm >= prev[0]:
            best[key] = (ts_norm, fval)

    return [(k[0], k[1], k[2], v[1]) for k, v in sorted(best.items())]


def compute_canonical_stats(
    values: Sequence[float], live_value: float, indicator: str
) -> CanonicalStats:
    """計算 `live_value` 在母體 `values` 中的 midrank 百分位與 robust Z。

    樣本少於 `CANONICAL_MIN_SAMPLES` 時百分位為 None（由呼叫端退回高頻池）。
    """
    n = len(values)
    if n < CANONICAL_MIN_SAMPLES:
        return CanonicalStats(None, n, None, False)

    # 同值採 midrank，與 history_storage 的高頻池一致：全同值時回到 50%，
    # 不會塌陷成 0%。
    count_less = sum(1 for v in values if v < live_value)
    count_equal = sum(1 for v in values if v == live_value)
    percentile = (count_less + 0.5 * count_equal) / n * 100.0

    robust_z: Optional[float] = None
    q25, _, q75 = statistics.quantiles(values, n=4)
    iqr = q75 - q25
    if iqr >= ROBUST_Z_MIN_IQR.get(indicator, 0.0) and iqr > 0:
        median_val = statistics.median(values)
        robust_z = round((live_value - median_val) / (iqr / _IQR_TO_SIGMA), 2)

    return CanonicalStats(
        round(percentile, 2), n, robust_z, n >= CANONICAL_MATURE_SAMPLES
    )


def query_canonical_values(
    cursor: Any,
    symbol: str,
    indicator: str,
    as_of_date: str,
    window_days: int = CANONICAL_WINDOW_DAYS,
) -> list[float]:
    """讀取 `as_of_date` **之前**（不含當日）最近 `window_days` 個交易日的規範值。

    排除當日是無前視偏差的要求：16:15 ET 寫入的當日收盤值不能拿來評估同一天
    稍早的盤中讀數。接受外部 cursor，讓呼叫端與高頻池查詢共用同一條連線。
    """
    cursor.execute(
        """
        SELECT value FROM sentiment_daily_canonical
        WHERE symbol = ? AND indicator = ? AND trade_date < ?
        ORDER BY trade_date DESC LIMIT ?
        """,
        (symbol.upper(), indicator, as_of_date, int(window_days)),
    )
    return [float(r[0]) for r in cursor.fetchall()]


def today_ny_str() -> str:
    """美東當日日期字串，作為線上評估的 `as_of_date` 預設值。"""
    return datetime.now(_NY_TZ).strftime("%Y-%m-%d")


_INSERT_SQL = """
    INSERT OR IGNORE INTO sentiment_daily_canonical
        (symbol, trade_date, indicator, value, source)
    VALUES (?, ?, ?, ?, ?)
"""


def build_insert_params(
    records: Sequence[tuple[str, str, str, float]], source: str
) -> list[tuple[str, str, str, float, str]]:
    return [(sym, day, ind, val, source) for sym, day, ind, val in records]


# IN 子句的佔位符只由 "?" 組成，實際值一律參數化。
_SESSION_ROWS_SQL = (
    "SELECT symbol, indicator, value, timestamp FROM sentiment_history "  # nosemgrep
    "WHERE timestamp >= ? AND timestamp <= ? "
    f"AND indicator IN ({','.join('?' for _ in CANONICAL_INDICATORS)}) "
    "ORDER BY id ASC"
)


def _read_session_rows(open_utc: str, close_utc: str) -> list[tuple[Any, ...]]:
    from database.connection import get_read_connection

    conn = get_read_connection()
    try:
        cursor = conn.cursor()
        cursor.execute(_SESSION_ROWS_SQL, (open_utc, close_utc, *CANONICAL_INDICATORS))
        return list(cursor.fetchall())
    finally:
        conn.close()


async def snapshot_trading_day(trade_date: str, source: str = "EOD_CLOSE") -> int:
    """把 `trade_date` 當日的盤中觀測重採樣後寫入規範母體。回傳新寫入的列數。

    冪等：已存在的 (symbol, trade_date, indicator) 不會被覆寫，因此 16:15 ET
    收盤快照與隔日 08:45 ET 補寫可以放心重複執行。非交易日回傳 0。
    """
    import asyncio

    from database.connection import execute_write_many_async
    from market_time import get_session_bounds_utc

    try:
        day = datetime.strptime(trade_date, "%Y-%m-%d").date()
        sessions = get_session_bounds_utc(day, day)
        bounds = sessions.get(trade_date)
        if bounds is None:
            return 0
        rows = await asyncio.to_thread(_read_session_rows, bounds[0], bounds[1])
        records = resample_daily_close(rows, sessions)
        if not records:
            return 0
        counts = await execute_write_many_async(
            [(_INSERT_SQL, build_insert_params(records, source), True)]
        )
        written = max(0, int(counts[0])) if counts else 0
        logger.info(
            f"📸 [Canonical 日級快照] {trade_date}：重採樣 {len(records)} 筆，"
            f"新寫入 {written} 筆 (source={source})。"
        )
        return written
    except Exception as e:
        logger.error(f"[Canonical 日級快照] {trade_date} 寫入失敗: {e}")
        return 0


async def purge_stale_canonical_history(
    retention_trading_days: int = CANONICAL_RETENTION_TRADING_DAYS,
) -> int:
    """保留期清理，由 03:00 ET 離峰排程呼叫。回傳實際刪除的列數。"""
    from database.connection import execute_write_rowcount_async
    from market_time import get_trading_days_ago_utc

    try:
        cutoff_utc = get_trading_days_ago_utc(retention_trading_days, for_purge=True)
        cutoff_date = cutoff_utc.split()[0]
        purged = await execute_write_rowcount_async(
            "DELETE FROM sentiment_daily_canonical WHERE trade_date < ?",
            (cutoff_date,),
        )
        return max(0, int(purged))
    except Exception as e:
        logger.error(f"sentiment_daily_canonical 保留期清理失敗: {e}")
        return 0


__all__ = [
    "CANONICAL_INDICATORS",
    "CANONICAL_MIN_SAMPLES",
    "CANONICAL_MATURE_SAMPLES",
    "CanonicalStats",
    "resample_daily_close",
    "compute_canonical_stats",
    "query_canonical_values",
    "snapshot_trading_day",
    "purge_stale_canonical_history",
]
