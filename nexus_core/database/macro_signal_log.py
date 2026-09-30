"""總經訊號乾跑記錄的存取層（寫入一律經 `database/connection.py`）。"""

from __future__ import annotations

import json
import logging
import sqlite3
from datetime import date
from typing import Any, Optional, Sequence

from database.connection import execute_write_many_async, get_read_connection
from market_analysis.macro_signals import (
    IndicatorReading,
    MacroState,
    Observation,
)

logger = logging.getLogger(__name__)

_INSERT_OBSERVATION_SQL = """
    INSERT OR IGNORE INTO macro_series_observation
        (series_id, obs_date, value, available_date, first_seen_date)
    VALUES (?, ?, ?, ?, ?)
"""

_UPSERT_SIGNAL_SQL = """
    INSERT OR REPLACE INTO macro_signal_log
        (trading_date, indicator, value, flag, as_of_date, available_date)
    VALUES (?, ?, ?, ?, ?, ?)
"""

_UPSERT_REGIME_SQL = """
    INSERT OR REPLACE INTO macro_regime_log
        (trading_date, raw_state, confirmed_state, flags_json)
    VALUES (?, ?, ?, ?)
"""


def _iso(d: Optional[date]) -> Optional[str]:
    return d.isoformat() if d is not None else None


async def store_observations(
    series_id: str, observations: Sequence[Observation], first_seen: date
) -> int:
    """寫入 FRED 觀測；已存在的 (series_id, obs_date) 不覆寫（保留首次所見值）。"""
    rows = [
        (
            series_id,
            o.obs_date.isoformat(),
            float(o.value),
            o.available_date.isoformat(),
            first_seen.isoformat(),
        )
        for o in observations
    ]
    if not rows:
        return 0
    counts = await execute_write_many_async([(_INSERT_OBSERVATION_SQL, rows, True)])
    return int(counts[0]) if counts else 0


def load_observations(series_id: str) -> list[Observation]:
    """讀取某序列的首次所見觀測（依觀測日排序）。"""
    conn = get_read_connection()
    try:
        rows = conn.execute(
            "SELECT obs_date, value, available_date FROM macro_series_observation "
            "WHERE series_id = ? ORDER BY obs_date",
            (series_id,),
        ).fetchall()
    finally:
        conn.close()
    return [
        Observation(date.fromisoformat(r[0]), float(r[1]), date.fromisoformat(r[2]))
        for r in rows
    ]


def load_recent_regimes(
    before: date, limit: int
) -> list[tuple[str, MacroState, Optional[MacroState]]]:
    """`before` 之前最近 `limit` 個交易日的 (日期, 原始狀態, 確認狀態)，依時間排序。"""
    conn = get_read_connection()
    try:
        rows = conn.execute(
            "SELECT trading_date, raw_state, confirmed_state FROM macro_regime_log "
            "WHERE trading_date < ? ORDER BY trading_date DESC LIMIT ?",
            (before.isoformat(), limit),
        ).fetchall()
    finally:
        conn.close()
    return [(r[0], r[1], r[2]) for r in reversed(rows)]


async def write_daily_log(
    trading_date: date,
    readings: Sequence[IndicatorReading],
    raw_state: MacroState,
    confirmed_state: Optional[MacroState],
) -> None:
    """同一交易日的全部指標與三態判定，單一交易寫入（冪等）。"""
    td = trading_date.isoformat()
    signal_rows = [
        (
            td,
            r.indicator.value,
            r.value,
            None if r.flag is None else int(r.flag),
            _iso(r.as_of_date),
            _iso(r.available_date),
        )
        for r in readings
    ]
    flags = {r.indicator.value: r.flag for r in readings}
    await execute_write_many_async(
        [
            (_UPSERT_SIGNAL_SQL, signal_rows, True),
            (
                _UPSERT_REGIME_SQL,
                (td, raw_state, confirmed_state, json.dumps(flags, sort_keys=True)),
            ),
        ]
    )


def load_regime_log(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    """離線報告用：以呼叫端提供的（唯讀）連線讀取全部三態判定。"""
    rows = conn.execute(
        "SELECT trading_date, raw_state, confirmed_state, flags_json "
        "FROM macro_regime_log ORDER BY trading_date"
    ).fetchall()
    return [
        {
            "trading_date": r[0],
            "raw_state": r[1],
            "confirmed_state": r[2],
            "flags": json.loads(r[3]) if r[3] else {},
        }
        for r in rows
    ]


def load_signal_log(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    """離線報告用：以呼叫端提供的（唯讀）連線讀取全部指標紀錄。"""
    rows = conn.execute(
        "SELECT trading_date, indicator, value, flag, as_of_date, available_date "
        "FROM macro_signal_log ORDER BY trading_date, indicator"
    ).fetchall()
    return [
        {
            "trading_date": r[0],
            "indicator": r[1],
            "value": r[2],
            "flag": None if r[3] is None else bool(r[3]),
            "as_of_date": r[4],
            "available_date": r[5],
        }
        for r in rows
    ]
