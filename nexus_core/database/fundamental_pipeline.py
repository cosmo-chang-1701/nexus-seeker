"""基本面分析管線資料庫持久層。

遵循 Nexus Seeker 資料庫單一寫入器架構（Single-Writer Invariant）：
- 寫入一律經由 `database.connection` 之 `execute_write_async` / `execute_write_many_async`。
- 讀取一律經由 `get_read_connection()` 並在 `try ... finally: conn.close()` 中確實釋放。
"""

from __future__ import annotations

import logging
from datetime import date

from market_analysis.fundamental_pipeline.models import (
    LiquidityReading,
    MacroSurpriseReading,
)

from database.connection import (
    execute_write_async,
    execute_write_many_async,
    get_read_connection,
)

logger = logging.getLogger(__name__)

_UPSERT_LIQUIDITY_REGIME_SQL = """
INSERT INTO liquidity_regime_log (
    trading_date,
    nfci,
    anfci,
    net_liquidity_bn,
    net_liquidity_chg_13w_pct,
    reserves_chg_13w_pct,
    us10y,
    regime,
    equity_risk_premium
) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
ON CONFLICT(trading_date) DO UPDATE SET
    nfci = excluded.nfci,
    anfci = excluded.anfci,
    net_liquidity_bn = excluded.net_liquidity_bn,
    net_liquidity_chg_13w_pct = excluded.net_liquidity_chg_13w_pct,
    reserves_chg_13w_pct = excluded.reserves_chg_13w_pct,
    us10y = excluded.us10y,
    regime = excluded.regime,
    equity_risk_premium = excluded.equity_risk_premium
"""

_UPSERT_MACRO_SURPRISE_SQL = """
INSERT INTO macro_release_surprise (
    event_key,
    release_time_utc,
    actual,
    forecast,
    raw_diff,
    z_score,
    growth_sign
) VALUES (?, ?, ?, ?, ?, ?, ?)
ON CONFLICT(event_key, release_time_utc) DO UPDATE SET
    actual = excluded.actual,
    forecast = excluded.forecast,
    raw_diff = excluded.raw_diff,
    z_score = excluded.z_score,
    growth_sign = excluded.growth_sign
"""


async def save_liquidity_regime(reading: LiquidityReading) -> None:
    """寫入或更新流動性體制讀數。"""
    params = (
        reading.trading_date.isoformat(),
        reading.nfci,
        reading.anfci,
        reading.net_liquidity_bn,
        reading.net_liquidity_chg_13w_pct,
        reading.reserves_chg_13w_pct,
        reading.us10y,
        reading.regime,
        reading.equity_risk_premium,
    )
    await execute_write_async(_UPSERT_LIQUIDITY_REGIME_SQL, params)


def get_latest_liquidity_regime() -> LiquidityReading | None:
    """讀取最新一筆流動性體制讀數。"""
    conn = get_read_connection()
    try:
        row = conn.execute(
            """
            SELECT trading_date, nfci, anfci, net_liquidity_bn,
                   net_liquidity_chg_13w_pct, reserves_chg_13w_pct,
                   us10y, regime, equity_risk_premium
            FROM liquidity_regime_log
            ORDER BY trading_date DESC
            LIMIT 1
            """
        ).fetchone()
        if not row:
            return None
        return LiquidityReading(
            trading_date=date.fromisoformat(row[0]),
            nfci=float(row[1]) if row[1] is not None else None,
            anfci=float(row[2]) if row[2] is not None else None,
            net_liquidity_bn=float(row[3]) if row[3] is not None else None,
            net_liquidity_chg_13w_pct=float(row[4]) if row[4] is not None else None,
            reserves_chg_13w_pct=float(row[5]) if row[5] is not None else None,
            us10y=float(row[6]) if row[6] is not None else None,
            regime=row[7],
            equity_risk_premium=float(row[8]) if row[8] is not None else None,
        )
    finally:
        conn.close()


def get_liquidity_regime_by_date(trading_date: date) -> LiquidityReading | None:
    """根據交易日讀取流動性體制讀數。"""
    conn = get_read_connection()
    try:
        row = conn.execute(
            """
            SELECT trading_date, nfci, anfci, net_liquidity_bn,
                   net_liquidity_chg_13w_pct, reserves_chg_13w_pct,
                   us10y, regime, equity_risk_premium
            FROM liquidity_regime_log
            WHERE trading_date = ?
            """,
            (trading_date.isoformat(),),
        ).fetchone()
        if not row:
            return None
        return LiquidityReading(
            trading_date=date.fromisoformat(row[0]),
            nfci=float(row[1]) if row[1] is not None else None,
            anfci=float(row[2]) if row[2] is not None else None,
            net_liquidity_bn=float(row[3]) if row[3] is not None else None,
            net_liquidity_chg_13w_pct=float(row[4]) if row[4] is not None else None,
            reserves_chg_13w_pct=float(row[5]) if row[5] is not None else None,
            us10y=float(row[6]) if row[6] is not None else None,
            regime=row[7],
            equity_risk_premium=float(row[8]) if row[8] is not None else None,
        )
    finally:
        conn.close()


async def save_macro_surprise(reading: MacroSurpriseReading) -> None:
    """寫入或更新宏觀預期差讀數。"""
    params = (
        reading.event_key,
        reading.release_time_utc,
        reading.actual,
        reading.forecast,
        reading.raw_diff,
        reading.z_score,
        reading.growth_sign,
    )
    await execute_write_async(_UPSERT_MACRO_SURPRISE_SQL, params)


async def save_macro_surprises(readings: list[MacroSurpriseReading]) -> None:
    """批次寫入宏觀預期差讀數。"""
    if not readings:
        return
    rows = [
        (
            r.event_key,
            r.release_time_utc,
            r.actual,
            r.forecast,
            r.raw_diff,
            r.z_score,
            r.growth_sign,
        )
        for r in readings
    ]
    await execute_write_many_async([(_UPSERT_MACRO_SURPRISE_SQL, rows, False)])


def get_macro_surprises_for_event(
    event_key: str, limit: int = 12
) -> list[MacroSurpriseReading]:
    """讀取某事件最近 N 期的歷史預期差（依發布時間升冪排序，最早在前最新在後）。"""
    conn = get_read_connection()
    try:
        rows = conn.execute(
            """
            SELECT event_key, release_time_utc, actual, forecast,
                   raw_diff, z_score, growth_sign
            FROM macro_release_surprise
            WHERE event_key = ?
            ORDER BY release_time_utc DESC
            LIMIT ?
            """,
            (event_key, limit),
        ).fetchall()
        readings = [
            MacroSurpriseReading(
                event_key=r[0],
                release_time_utc=r[1],
                actual=float(r[2]),
                forecast=float(r[3]),
                raw_diff=float(r[4]),
                z_score=float(r[5]) if r[5] is not None else None,
                growth_sign=int(r[6]),
            )
            for r in reversed(rows)
        ]
        return readings
    finally:
        conn.close()


def get_latest_macro_surprises(limit: int = 20) -> list[MacroSurpriseReading]:
    """讀取最新發布的宏觀預期差清單。"""
    conn = get_read_connection()
    try:
        rows = conn.execute(
            """
            SELECT event_key, release_time_utc, actual, forecast,
                   raw_diff, z_score, growth_sign
            FROM macro_release_surprise
            ORDER BY release_time_utc DESC
            LIMIT ?
            """,
            (limit,),
        ).fetchall()
        return [
            MacroSurpriseReading(
                event_key=r[0],
                release_time_utc=r[1],
                actual=float(r[2]),
                forecast=float(r[3]),
                raw_diff=float(r[4]),
                z_score=float(r[5]) if r[5] is not None else None,
                growth_sign=int(r[6]),
            )
            for r in rows
        ]
    finally:
        conn.close()
