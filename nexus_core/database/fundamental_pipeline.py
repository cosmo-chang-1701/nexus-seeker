"""基本面分析管線資料庫持久層。

遵循 Nexus Seeker 資料庫單一寫入器架構（Single-Writer Invariant）：
- 寫入一律經由 `database.connection` 之 `execute_write_async` / `execute_write_many_async`。
- 讀取一律經由 `get_read_connection()` 並在 `try ... finally: conn.close()` 中確實釋放。
"""

from __future__ import annotations

import logging
import re
from datetime import date, datetime, timedelta, timezone
from typing import cast

from market_analysis.fundamental_pipeline.models import (
    FilingCursorRecord,
    FilingEventRecord,
    FilingSession,
    GovernanceFlagRecord,
    GovernanceSeverity,
    InsiderTxRecord,
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
    await execute_write_many_async([(_UPSERT_MACRO_SURPRISE_SQL, rows, True)])


def get_macro_surprises_for_event(
    event_key: str, limit: int = 12, before: str | None = None
) -> list[MacroSurpriseReading]:
    """讀取某事件最近 N 期的歷史預期差（依發布時間升冪排序，最早在前最新在後）。

    `before`：僅取 `release_time_utc < before` 的樣本（ISO-8601 UTC 字串，與
    `release_time_utc` 同格式，字典序即時間序）。計算某次發布的 Z 分數時必須傳入
    該發布時間，避免引用事件之後才公布的資料（前視偏差）。
    """
    conn = get_read_connection()
    try:
        if before is None:
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
        else:
            rows = conn.execute(
                """
                SELECT event_key, release_time_utc, actual, forecast,
                       raw_diff, z_score, growth_sign
                FROM macro_release_surprise
                WHERE event_key = ?
                  AND release_time_utc < ?
                ORDER BY release_time_utc DESC
                LIMIT ?
                """,
                (event_key, before, limit),
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


def get_recorded_macro_surprise_keys(since: str) -> set[tuple[str, str]]:
    """讀取 `release_time_utc >= since` 已入庫的 (event_key, release_time_utc) 集合。

    供預期差服務略過已計算過的發布，避免以事後資料重算覆寫既有 Z 分數。
    """
    conn = get_read_connection()
    try:
        rows = conn.execute(
            """
            SELECT event_key, release_time_utc
            FROM macro_release_surprise
            WHERE release_time_utc >= ?
            """,
            (since,),
        ).fetchall()
        return {(str(r[0]), str(r[1])) for r in rows}
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


# ============================================================================
# PR2: SEC EDGAR 申報游標、事件流、內部人交易與治理審查旗標
# ============================================================================

_UPSERT_SEC_FILING_CURSOR_SQL = """
INSERT INTO sec_filing_cursor (
    symbol,
    cik,
    last_accepted_at,
    last_accession,
    updated_at
) VALUES (?, ?, ?, ?, CURRENT_TIMESTAMP)
ON CONFLICT(symbol) DO UPDATE SET
    cik = excluded.cik,
    last_accepted_at = excluded.last_accepted_at,
    last_accession = excluded.last_accession,
    updated_at = CURRENT_TIMESTAMP
"""

_UPSERT_SEC_FILING_EVENT_SQL = """
INSERT INTO sec_filing_event (
    accession,
    symbol,
    form,
    items,
    accepted_at,
    session,
    primary_doc_url,
    routes_json,
    is_backfill
) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
ON CONFLICT(accession) DO UPDATE SET
    symbol = excluded.symbol,
    form = excluded.form,
    items = excluded.items,
    accepted_at = excluded.accepted_at,
    session = excluded.session,
    primary_doc_url = excluded.primary_doc_url,
    routes_json = excluded.routes_json,
    is_backfill = excluded.is_backfill
"""

_UPSERT_INSIDER_TX_SQL = """
INSERT INTO insider_transaction (
    accession,
    line_no,
    symbol,
    owner_name,
    owner_role,
    is_c_suite,
    tx_date,
    tx_code,
    shares,
    price,
    acquired_disposed,
    shares_after,
    is_10b5_1,
    is_backfill
) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
ON CONFLICT(accession, line_no) DO UPDATE SET
    symbol = excluded.symbol,
    owner_name = excluded.owner_name,
    owner_role = excluded.owner_role,
    is_c_suite = excluded.is_c_suite,
    tx_date = excluded.tx_date,
    tx_code = excluded.tx_code,
    shares = excluded.shares,
    price = excluded.price,
    acquired_disposed = excluded.acquired_disposed,
    shares_after = excluded.shares_after,
    is_10b5_1 = excluded.is_10b5_1,
    is_backfill = excluded.is_backfill
"""

_UPSERT_GOVERNANCE_FLAG_SQL = """
INSERT INTO governance_flag (
    symbol,
    source_accession,
    flag_kind,
    severity,
    detail_json,
    expires_at
) VALUES (?, ?, ?, ?, ?, ?)
ON CONFLICT(symbol, source_accession, flag_kind) DO UPDATE SET
    severity = excluded.severity,
    detail_json = excluded.detail_json,
    expires_at = excluded.expires_at
"""


async def upsert_sec_filing_cursor(cursor: FilingCursorRecord) -> None:
    """寫入或更新 SEC 申報游標。"""
    params = (
        cursor.symbol,
        cursor.cik,
        cursor.last_accepted_at,
        cursor.last_accession,
    )
    await execute_write_async(_UPSERT_SEC_FILING_CURSOR_SQL, params)


def get_sec_filing_cursor(symbol: str) -> FilingCursorRecord | None:
    """查詢某標的的 SEC 申報游標。"""
    conn = get_read_connection()
    try:
        row = conn.execute(
            """
            SELECT symbol, cik, last_accepted_at, last_accession, updated_at
            FROM sec_filing_cursor
            WHERE symbol = ?
            """,
            (symbol.upper(),),
        ).fetchone()
        if not row:
            return None
        return FilingCursorRecord(
            symbol=row[0],
            cik=row[1],
            last_accepted_at=row[2],
            last_accession=row[3],
            updated_at=row[4] if row[4] else "",
        )
    finally:
        conn.close()


async def save_sec_filing_event(event: FilingEventRecord) -> None:
    """寫入單筆 SEC 申報事件。"""
    params = (
        event.accession,
        event.symbol.upper(),
        event.form,
        event.items,
        event.accepted_at,
        event.session,
        event.primary_doc_url,
        event.routes_json,
        1 if event.is_backfill else 0,
    )
    await execute_write_async(_UPSERT_SEC_FILING_EVENT_SQL, params)


async def save_sec_filing_events(events: list[FilingEventRecord]) -> None:
    """批次寫入 SEC 申報事件。"""
    if not events:
        return
    rows = [
        (
            e.accession,
            e.symbol.upper(),
            e.form,
            e.items,
            e.accepted_at,
            e.session,
            e.primary_doc_url,
            e.routes_json,
            1 if e.is_backfill else 0,
        )
        for e in events
    ]
    await execute_write_many_async([(_UPSERT_SEC_FILING_EVENT_SQL, rows, True)])


def get_recent_sec_events(symbol: str, limit: int = 20) -> list[FilingEventRecord]:
    """讀取某標的最近 N 筆 SEC 申報事件（依 accepted_at 降冪排序）。"""
    conn = get_read_connection()
    try:
        rows = conn.execute(
            """
            SELECT accession, symbol, form, items, accepted_at, session,
                   primary_doc_url, routes_json, is_backfill, created_at
            FROM sec_filing_event
            WHERE symbol = ?
            ORDER BY accepted_at DESC
            LIMIT ?
            """,
            (symbol.upper(), limit),
        ).fetchall()
        return [
            FilingEventRecord(
                accession=r[0],
                symbol=r[1],
                form=r[2],
                items=r[3],
                accepted_at=r[4],
                session=cast(FilingSession, r[5]),
                primary_doc_url=r[6],
                routes_json=r[7],
                is_backfill=bool(r[8]),
                created_at=r[9] if r[9] else "",
            )
            for r in rows
        ]
    finally:
        conn.close()


_OWNER_CIK_FIELD_RE = re.compile(r"^(\d{10})\|(.*)$", re.DOTALL)


def _encode_owner_field(owner_name: str, owner_cik: str | None) -> str:
    """把主申報人 CIK 編碼進 insider_transaction.owner_name 欄（`{cik}|{names}`）。

    v091 已部署至正式 DB 且不可修改，後續 migration 編號已被其他分支占用，因此不新增
    欄位，改以固定前綴保存 CIK；無 CIK 時原樣保存名稱。
    """
    if owner_cik:
        return f"{owner_cik}|{owner_name}"
    return owner_name


def _decode_owner_field(raw: str) -> tuple[str, str | None]:
    """還原 `_encode_owner_field` 的編碼，回傳 (名稱, CIK 或 None)。"""
    match = _OWNER_CIK_FIELD_RE.match(raw or "")
    if match is None:
        return raw, None
    return match.group(2), match.group(1)


async def save_insider_transactions(txs: list[InsiderTxRecord]) -> None:
    """批次寫入內部人交易明細。"""
    if not txs:
        return
    rows = [
        (
            t.accession,
            t.line_no,
            t.symbol.upper(),
            _encode_owner_field(t.owner_name, t.owner_cik),
            t.owner_role,
            1 if t.is_c_suite else 0,
            t.tx_date,
            t.tx_code,
            t.shares,
            t.price,
            t.acquired_disposed,
            t.shares_after,
            1 if t.is_10b5_1 else 0,
            1 if t.is_backfill else 0,
        )
        for t in txs
    ]
    await execute_write_many_async([(_UPSERT_INSIDER_TX_SQL, rows, True)])


def get_insider_transactions(symbol: str, days: int = 90) -> list[InsiderTxRecord]:
    """讀取某標的最近 N 天內的內部人交易記錄。

    LEFT JOIN sec_filing_event 以還原所屬申報的 form（判斷 Form 4/A）與受理時間，
    供 `insider_signal.dedupe_amended_transactions` 以修正申報取代原始明細。
    """
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).strftime("%Y-%m-%d")
    conn = get_read_connection()
    try:
        rows = conn.execute(
            """
            SELECT t.accession, t.line_no, t.symbol, t.owner_name, t.owner_role,
                   t.is_c_suite, t.tx_date, t.tx_code, t.shares, t.price,
                   t.acquired_disposed, t.shares_after, t.is_10b5_1, t.is_backfill,
                   t.created_at, e.form, e.accepted_at
            FROM insider_transaction AS t
            LEFT JOIN sec_filing_event AS e ON e.accession = t.accession
            WHERE t.symbol = ? AND t.tx_date >= ?
            ORDER BY t.tx_date DESC, t.line_no ASC
            """,
            (symbol.upper(), cutoff),
        ).fetchall()
        records: list[InsiderTxRecord] = []
        for r in rows:
            owner_name, owner_cik = _decode_owner_field(r[3])
            form = str(r[15]).strip().upper() if r[15] else ""
            records.append(
                InsiderTxRecord(
                    accession=r[0],
                    line_no=int(r[1]),
                    symbol=r[2],
                    owner_name=owner_name,
                    owner_cik=owner_cik,
                    owner_role=r[4] if r[4] else "OTHER",
                    is_c_suite=bool(r[5]),
                    tx_date=r[6],
                    tx_code=r[7],
                    shares=float(r[8]) if r[8] is not None else 0.0,
                    price=float(r[9]) if r[9] is not None else 0.0,
                    acquired_disposed=r[10] if r[10] else "D",
                    shares_after=float(r[11]) if r[11] is not None else 0.0,
                    is_10b5_1=bool(r[12]),
                    is_backfill=bool(r[13]),
                    created_at=r[14] if r[14] else "",
                    is_amendment=form.endswith("/A"),
                    filing_accepted_at=r[16] if r[16] else "",
                )
            )
        return records
    finally:
        conn.close()


async def save_governance_flags(flags: list[GovernanceFlagRecord]) -> None:
    """批次寫入治理審查旗標。"""
    if not flags:
        return
    rows = [
        (
            f.symbol.upper(),
            f.source_accession,
            f.flag_kind,
            f.severity,
            f.detail_json,
            f.expires_at,
        )
        for f in flags
    ]
    await execute_write_many_async([(_UPSERT_GOVERNANCE_FLAG_SQL, rows, True)])


def get_active_governance_flags(
    symbol: str, as_of: str | None = None
) -> list[GovernanceFlagRecord]:
    """讀取某標的當前仍處於生效期內的治理審查旗標。"""
    if as_of is None:
        as_of = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    conn = get_read_connection()
    try:
        rows = conn.execute(
            """
            SELECT symbol, source_accession, flag_kind, severity, detail_json,
                   expires_at, created_at
            FROM governance_flag
            WHERE symbol = ? AND expires_at >= ?
            ORDER BY created_at DESC
            """,
            (symbol.upper(), as_of),
        ).fetchall()
        return [
            GovernanceFlagRecord(
                symbol=r[0],
                source_accession=r[1],
                flag_kind=r[2],
                severity=cast(GovernanceSeverity, r[3]),
                detail_json=r[4],
                expires_at=r[5],
                created_at=r[6] if r[6] else "",
            )
            for r in rows
        ]
    finally:
        conn.close()
