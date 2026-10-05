"""基本面分析管線資料庫持久層。

遵循 Nexus Seeker 資料庫單一寫入器架構（Single-Writer Invariant）：
- 寫入一律經由 `database.connection` 之 `execute_write_async` / `execute_write_many_async`。
- 讀取一律經由 `get_read_connection()` 並在 `try ... finally: conn.close()` 中確實釋放。
"""

from __future__ import annotations

import logging
from datetime import date, datetime, timedelta, timezone
from typing import Any, cast

from market_analysis.fundamental_pipeline.models import (
    ChannelCheckLogRecord,
    ChannelCheckVerdict,
    EarningsSurpriseDTO,
    EarningsSurpriseStatus,
    EpsBasis,
    EPSEstimateSnapshotRecord,
    EstimateHorizon,
    FilingCursorRecord,
    FilingEventRecord,
    FilingSession,
    GovernanceFlagRecord,
    GovernanceSeverity,
    GuidanceExtractionDTO,
    InsiderTxRecord,
    LinkType,
    LiquidityReading,
    MacroSurpriseReading,
    NowcastDirection,
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


async def save_insider_transactions(txs: list[InsiderTxRecord]) -> None:
    """批次寫入內部人交易明細。"""
    if not txs:
        return
    rows = [
        (
            t.accession,
            t.line_no,
            t.symbol.upper(),
            t.owner_name,
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
    """讀取某標的最近 N 天內的內部人交易記錄。"""
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).strftime("%Y-%m-%d")
    conn = get_read_connection()
    try:
        rows = conn.execute(
            """
            SELECT accession, line_no, symbol, owner_name, owner_role, is_c_suite,
                   tx_date, tx_code, shares, price, acquired_disposed, shares_after,
                   is_10b5_1, is_backfill, created_at
            FROM insider_transaction
            WHERE symbol = ? AND tx_date >= ?
            ORDER BY tx_date DESC, line_no ASC
            """,
            (symbol.upper(), cutoff),
        ).fetchall()
        return [
            InsiderTxRecord(
                accession=r[0],
                line_no=int(r[1]),
                symbol=r[2],
                owner_name=r[3],
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
            )
            for r in rows
        ]
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


# ============================================================================
# PR3 財務預期差、分析師共識快照與管理層指引持久層實作
# ============================================================================

_UPSERT_EARNINGS_SURPRISE_SQL = """
INSERT INTO earnings_surprise (
    symbol,
    fiscal_period,
    actual_eps,
    consensus_eps,
    eps_surprise_pct,
    actual_revenue,
    consensus_revenue,
    revenue_surprise_pct,
    whisper_eps,
    composite_score,
    session,
    eps_basis,
    status
) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
ON CONFLICT(symbol, fiscal_period) DO UPDATE SET
    actual_eps = excluded.actual_eps,
    consensus_eps = excluded.consensus_eps,
    eps_surprise_pct = excluded.eps_surprise_pct,
    actual_revenue = excluded.actual_revenue,
    consensus_revenue = excluded.consensus_revenue,
    revenue_surprise_pct = excluded.revenue_surprise_pct,
    whisper_eps = excluded.whisper_eps,
    composite_score = excluded.composite_score,
    session = excluded.session,
    eps_basis = excluded.eps_basis,
    status = excluded.status
"""

_UPSERT_EPS_ESTIMATE_SNAPSHOT_SQL = """
INSERT INTO eps_estimate_snapshot (
    symbol,
    snapshot_date,
    horizon,
    source,
    eps_mean,
    eps_high,
    eps_low,
    analyst_count
) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
ON CONFLICT(symbol, snapshot_date, horizon, source) DO UPDATE SET
    eps_mean = excluded.eps_mean,
    eps_high = excluded.eps_high,
    eps_low = excluded.eps_low,
    analyst_count = excluded.analyst_count
"""

_UPSERT_GUIDANCE_EXTRACTION_SQL = """
INSERT INTO guidance_extraction (
    symbol,
    fiscal_period,
    source_accession,
    model_version,
    confidence_score,
    tone_delta_score,
    data_json
) VALUES (?, ?, ?, ?, ?, ?, ?)
ON CONFLICT(symbol, fiscal_period) DO UPDATE SET
    source_accession = excluded.source_accession,
    model_version = excluded.model_version,
    confidence_score = excluded.confidence_score,
    tone_delta_score = excluded.tone_delta_score,
    data_json = excluded.data_json
"""


async def save_earnings_surprise(surprise: EarningsSurpriseDTO) -> None:
    """寫入或更新單筆財務預期差記錄。"""
    params = (
        surprise.symbol.upper(),
        surprise.fiscal_period,
        surprise.actual_eps,
        surprise.consensus_eps,
        surprise.eps_surprise_pct,
        surprise.actual_revenue,
        surprise.consensus_revenue,
        surprise.revenue_surprise_pct,
        surprise.whisper_eps,
        surprise.composite_score,
        surprise.session,
        surprise.eps_basis,
        surprise.status,
    )
    await execute_write_async(_UPSERT_EARNINGS_SURPRISE_SQL, params)


async def save_earnings_surprises(surprises: list[EarningsSurpriseDTO]) -> None:
    """批次寫入或更新財務預期差記錄。"""
    if not surprises:
        return
    rows = [
        (
            s.symbol.upper(),
            s.fiscal_period,
            s.actual_eps,
            s.consensus_eps,
            s.eps_surprise_pct,
            s.actual_revenue,
            s.consensus_revenue,
            s.revenue_surprise_pct,
            s.whisper_eps,
            s.composite_score,
            s.session,
            s.eps_basis,
            s.status,
        )
        for s in surprises
    ]
    await execute_write_many_async([(_UPSERT_EARNINGS_SURPRISE_SQL, rows, True)])


def get_earnings_surprise(
    symbol: str, fiscal_period: str
) -> EarningsSurpriseDTO | None:
    """讀取特定標的與季度之財務預期差記錄。"""
    conn = get_read_connection()
    try:
        row = conn.execute(
            """
            SELECT symbol, fiscal_period, actual_eps, consensus_eps,
                   eps_surprise_pct, actual_revenue, consensus_revenue,
                   revenue_surprise_pct, whisper_eps, composite_score,
                   session, eps_basis, status, created_at
            FROM earnings_surprise
            WHERE symbol = ? AND fiscal_period = ?
            """,
            (symbol.upper(), fiscal_period),
        ).fetchone()
        if not row:
            return None
        return EarningsSurpriseDTO(
            symbol=row[0],
            fiscal_period=row[1],
            actual_eps=row[2],
            consensus_eps=row[3],
            eps_surprise_pct=row[4],
            actual_revenue=row[5],
            consensus_revenue=row[6],
            revenue_surprise_pct=row[7],
            whisper_eps=row[8],
            composite_score=row[9],
            session=row[10],
            eps_basis=cast(EpsBasis, row[11]),
            status=cast(EarningsSurpriseStatus, row[12]),
            created_at=row[13] if row[13] else "",
        )
    finally:
        conn.close()


def get_latest_earnings_surprise(symbol: str) -> EarningsSurpriseDTO | None:
    """讀取特定標的最新一筆財務預期差記錄。"""
    conn = get_read_connection()
    try:
        row = conn.execute(
            """
            SELECT symbol, fiscal_period, actual_eps, consensus_eps,
                   eps_surprise_pct, actual_revenue, consensus_revenue,
                   revenue_surprise_pct, whisper_eps, composite_score,
                   session, eps_basis, status, created_at
            FROM earnings_surprise
            WHERE symbol = ?
            ORDER BY fiscal_period DESC, created_at DESC
            LIMIT 1
            """,
            (symbol.upper(),),
        ).fetchone()
        if not row:
            return None
        return EarningsSurpriseDTO(
            symbol=row[0],
            fiscal_period=row[1],
            actual_eps=row[2],
            consensus_eps=row[3],
            eps_surprise_pct=row[4],
            actual_revenue=row[5],
            consensus_revenue=row[6],
            revenue_surprise_pct=row[7],
            whisper_eps=row[8],
            composite_score=row[9],
            session=row[10],
            eps_basis=cast(EpsBasis, row[11]),
            status=cast(EarningsSurpriseStatus, row[12]),
            created_at=row[13] if row[13] else "",
        )
    finally:
        conn.close()


async def save_eps_estimate_snapshots(
    snapshots: list[EPSEstimateSnapshotRecord],
) -> None:
    """批次寫入分析師每股盈餘預估快照。"""
    if not snapshots:
        return
    rows = [
        (
            s.symbol.upper(),
            s.snapshot_date,
            s.horizon,
            s.source,
            s.eps_mean,
            s.eps_high,
            s.eps_low,
            s.analyst_count,
        )
        for s in snapshots
    ]
    await execute_write_many_async([(_UPSERT_EPS_ESTIMATE_SNAPSHOT_SQL, rows, True)])


def get_eps_estimate_snapshots(
    symbol: str, snapshot_date: str | None = None
) -> list[EPSEstimateSnapshotRecord]:
    """讀取特定標的之分析師預估快照（預設取最新日期）。"""
    conn = get_read_connection()
    try:
        if snapshot_date is None:
            max_row = conn.execute(
                """
                SELECT MAX(snapshot_date) FROM eps_estimate_snapshot
                WHERE symbol = ?
                """,
                (symbol.upper(),),
            ).fetchone()
            if not max_row or not max_row[0]:
                return []
            target_date = max_row[0]
        else:
            target_date = snapshot_date

        rows = conn.execute(
            """
            SELECT symbol, snapshot_date, horizon, source,
                   eps_mean, eps_high, eps_low, analyst_count, created_at
            FROM eps_estimate_snapshot
            WHERE symbol = ? AND snapshot_date = ?
            ORDER BY CASE horizon WHEN '0q' THEN 1 WHEN '+1q' THEN 2 WHEN '0y' THEN 3 WHEN '+1y' THEN 4 ELSE 5 END ASC
            """,
            (symbol.upper(), target_date),
        ).fetchall()
        return [
            EPSEstimateSnapshotRecord(
                symbol=r[0],
                snapshot_date=r[1],
                horizon=cast(EstimateHorizon, r[2]),
                source=r[3],
                eps_mean=float(r[4]),
                eps_high=float(r[5]) if r[5] is not None else None,
                eps_low=float(r[6]) if r[6] is not None else None,
                analyst_count=int(r[7]) if r[7] is not None else None,
                created_at=r[8] if r[8] else "",
            )
            for r in rows
        ]
    finally:
        conn.close()


async def save_guidance_extraction(extraction: GuidanceExtractionDTO) -> None:
    """寫入或更新單筆管理層前瞻指引擷取記錄。"""
    params = (
        extraction.symbol.upper(),
        extraction.fiscal_period,
        extraction.source_accession,
        extraction.model_version,
        extraction.confidence_score,
        extraction.tone_delta_score,
        extraction.data_json,
    )
    await execute_write_async(_UPSERT_GUIDANCE_EXTRACTION_SQL, params)


def get_guidance_extraction(
    symbol: str, fiscal_period: str
) -> GuidanceExtractionDTO | None:
    """讀取特定標的與季度之管理層指引記錄。"""
    conn = get_read_connection()
    try:
        row = conn.execute(
            """
            SELECT symbol, fiscal_period, source_accession, model_version,
                   confidence_score, tone_delta_score, data_json, created_at
            FROM guidance_extraction
            WHERE symbol = ? AND fiscal_period = ?
            """,
            (symbol.upper(), fiscal_period),
        ).fetchone()
        if not row:
            return None
        return GuidanceExtractionDTO(
            symbol=row[0],
            fiscal_period=row[1],
            source_accession=row[2],
            model_version=row[3],
            confidence_score=float(row[4]),
            tone_delta_score=float(row[5]),
            data_json=row[6],
            created_at=row[7] if row[7] else "",
        )
    finally:
        conn.close()


def get_latest_guidance_extraction(symbol: str) -> GuidanceExtractionDTO | None:
    """讀取特定標的最新一筆管理層指引記錄。"""
    conn = get_read_connection()
    try:
        row = conn.execute(
            """
            SELECT symbol, fiscal_period, source_accession, model_version,
                   confidence_score, tone_delta_score, data_json, created_at
            FROM guidance_extraction
            WHERE symbol = ?
            ORDER BY fiscal_period DESC, created_at DESC
            LIMIT 1
            """,
            (symbol.upper(),),
        ).fetchone()
        if not row:
            return None
        return GuidanceExtractionDTO(
            symbol=row[0],
            fiscal_period=row[1],
            source_accession=row[2],
            model_version=row[3],
            confidence_score=float(row[4]),
            tone_delta_score=float(row[5]),
            data_json=row[6],
            created_at=row[7] if row[7] else "",
        )
    finally:
        conn.close()


# ============================================================================
# PR4 實體產業鏈交叉驗證持久化
# ============================================================================

_UPSERT_CHANNEL_CHECK_LOG_SQL = """
INSERT INTO channel_check_log (
    link_key,
    as_of_period,
    link_type,
    experimental,
    driver_growth,
    follower_growth,
    divergence_pp,
    nowcast_direction,
    nowcast_hit,
    correlation,
    verdict,
    members_json
) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
ON CONFLICT(link_key, as_of_period) DO UPDATE SET
    link_type = excluded.link_type,
    experimental = excluded.experimental,
    driver_growth = excluded.driver_growth,
    follower_growth = excluded.follower_growth,
    divergence_pp = excluded.divergence_pp,
    nowcast_direction = excluded.nowcast_direction,
    nowcast_hit = excluded.nowcast_hit,
    correlation = excluded.correlation,
    verdict = excluded.verdict,
    members_json = excluded.members_json
"""


async def save_channel_check_log(record: ChannelCheckLogRecord) -> None:
    """寫入或更新單筆產業鏈交叉驗證日誌。"""
    params = (
        record.link_key,
        record.as_of_period,
        record.link_type,
        1 if record.experimental else 0,
        record.driver_growth,
        record.follower_growth,
        record.divergence_pp,
        record.nowcast_direction,
        (1 if record.nowcast_hit else 0) if record.nowcast_hit is not None else None,
        record.correlation,
        record.verdict,
        record.members_json,
    )
    await execute_write_async(_UPSERT_CHANNEL_CHECK_LOG_SQL, params)


async def save_channel_check_logs(records: list[ChannelCheckLogRecord]) -> None:
    """批次寫入產業鏈交叉驗證日誌（支援分批分塊防止 1GB VPS 記憶體暴增）。"""
    if not records:
        return
    rows = [
        (
            r.link_key,
            r.as_of_period,
            r.link_type,
            1 if r.experimental else 0,
            r.driver_growth,
            r.follower_growth,
            r.divergence_pp,
            r.nowcast_direction,
            (1 if r.nowcast_hit else 0) if r.nowcast_hit is not None else None,
            r.correlation,
            r.verdict,
            r.members_json,
        )
        for r in records
    ]
    # 分塊 100 筆寫入，符合低記憶體守衛
    chunk_size = 100
    for i in range(0, len(rows), chunk_size):
        chunk = rows[i : i + chunk_size]
        await execute_write_many_async([(_UPSERT_CHANNEL_CHECK_LOG_SQL, chunk, True)])


def get_channel_check(link_key: str, as_of_period: str) -> ChannelCheckLogRecord | None:
    """讀取特定鏈條與週期之產業鏈交叉驗證記錄。"""
    conn = get_read_connection()
    try:
        row = conn.execute(
            """
            SELECT link_key, as_of_period, link_type, experimental,
                   driver_growth, follower_growth, divergence_pp,
                   nowcast_direction, nowcast_hit, correlation,
                   verdict, members_json, created_at
            FROM channel_check_log
            WHERE link_key = ? AND as_of_period = ?
            """,
            (link_key, as_of_period),
        ).fetchone()
        if not row:
            return None
        return ChannelCheckLogRecord(
            link_key=row[0],
            as_of_period=row[1],
            link_type=cast(LinkType, row[2]),
            experimental=bool(row[3]),
            driver_growth=float(row[4]) if row[4] is not None else None,
            follower_growth=float(row[5]) if row[5] is not None else None,
            divergence_pp=float(row[6]) if row[6] is not None else None,
            nowcast_direction=cast(NowcastDirection, row[7])
            if row[7] is not None
            else None,
            nowcast_hit=bool(row[8]) if row[8] is not None else None,
            correlation=float(row[9]) if row[9] is not None else None,
            verdict=cast(ChannelCheckVerdict, row[10]),
            members_json=row[11],
            created_at=row[12] if row[12] else "",
        )
    finally:
        conn.close()


def get_channel_checks_for_period(
    as_of_period: str,
) -> list[ChannelCheckLogRecord]:
    """讀取某週期所有產業鏈交叉驗證記錄。"""
    conn = get_read_connection()
    try:
        rows = conn.execute(
            """
            SELECT link_key, as_of_period, link_type, experimental,
                   driver_growth, follower_growth, divergence_pp,
                   nowcast_direction, nowcast_hit, correlation,
                   verdict, members_json, created_at
            FROM channel_check_log
            WHERE as_of_period = ?
            ORDER BY link_key ASC
            """,
            (as_of_period,),
        ).fetchall()
        return [
            ChannelCheckLogRecord(
                link_key=r[0],
                as_of_period=r[1],
                link_type=cast(LinkType, r[2]),
                experimental=bool(r[3]),
                driver_growth=float(r[4]) if r[4] is not None else None,
                follower_growth=float(r[5]) if r[5] is not None else None,
                divergence_pp=float(r[6]) if r[6] is not None else None,
                nowcast_direction=cast(NowcastDirection, r[7])
                if r[7] is not None
                else None,
                nowcast_hit=bool(r[8]) if r[8] is not None else None,
                correlation=float(r[9]) if r[9] is not None else None,
                verdict=cast(ChannelCheckVerdict, r[10]),
                members_json=r[11],
                created_at=r[12] if r[12] else "",
            )
            for r in rows
        ]
    finally:
        conn.close()


def get_latest_channel_checks(
    verdict: str | None = None, limit: int = 50
) -> list[ChannelCheckLogRecord]:
    """讀取最新發布的產業鏈交叉驗證日誌清單。"""
    conn = get_read_connection()
    try:
        if verdict is not None:
            rows = conn.execute(
                """
                SELECT link_key, as_of_period, link_type, experimental,
                       driver_growth, follower_growth, divergence_pp,
                       nowcast_direction, nowcast_hit, correlation,
                       verdict, members_json, created_at
                FROM channel_check_log
                WHERE verdict = ?
                ORDER BY as_of_period DESC, created_at DESC
                LIMIT ?
                """,
                (verdict, limit),
            ).fetchall()
        else:
            rows = conn.execute(
                """
                SELECT link_key, as_of_period, link_type, experimental,
                       driver_growth, follower_growth, divergence_pp,
                       nowcast_direction, nowcast_hit, correlation,
                       verdict, members_json, created_at
                FROM channel_check_log
                ORDER BY as_of_period DESC, created_at DESC
                LIMIT ?
                """,
                (limit,),
            ).fetchall()
        return [
            ChannelCheckLogRecord(
                link_key=r[0],
                as_of_period=r[1],
                link_type=cast(LinkType, r[2]),
                experimental=bool(r[3]),
                driver_growth=float(r[4]) if r[4] is not None else None,
                follower_growth=float(r[5]) if r[5] is not None else None,
                divergence_pp=float(r[6]) if r[6] is not None else None,
                nowcast_direction=cast(NowcastDirection, r[7])
                if r[7] is not None
                else None,
                nowcast_hit=bool(r[8]) if r[8] is not None else None,
                correlation=float(r[9]) if r[9] is not None else None,
                verdict=cast(ChannelCheckVerdict, r[10]),
                members_json=r[11],
                created_at=r[12] if r[12] else "",
            )
            for r in rows
        ]
    finally:
        conn.close()


def get_channel_checks_by_symbol(
    symbol: str, limit: int = 20
) -> list[ChannelCheckLogRecord]:
    """根據標的代碼（包含在上游驅動或下游跟隨端）查詢相關之最新交叉驗證日誌。"""
    from market_analysis.fundamental_pipeline.supply_chain_map import (
        get_links_for_symbol,
    )

    clean_sym = symbol.strip().upper()
    if ":" in clean_sym:
        clean_sym = clean_sym.split(":")[-1]

    # 取得靜態映射中包含該標的之鏈條代碼
    related_links = get_links_for_symbol(clean_sym)
    link_keys = [link.link_key for link in related_links]

    conn = get_read_connection()
    try:
        quoted_pattern = f'%"{clean_sym}"%'
        raw_pattern = f"%{clean_sym}%"

        if link_keys:
            placeholders = ",".join("?" for _ in link_keys)
            query = f"""
            SELECT link_key, as_of_period, link_type, experimental,
                   driver_growth, follower_growth, divergence_pp,
                   nowcast_direction, nowcast_hit, correlation,
                   verdict, members_json, created_at
            FROM channel_check_log
            WHERE link_key IN ({placeholders})
               OR members_json LIKE ?
               OR members_json LIKE ?
            ORDER BY as_of_period DESC, created_at DESC
            LIMIT ?
            """
            params: list[Any] = [*link_keys, quoted_pattern, raw_pattern, limit]
            # nosemgrep: python.lang.security.audit.formatted-sql-query.formatted-sql-query, python.sqlalchemy.security.sqlalchemy-execute-raw-query.sqlalchemy-execute-raw-query
            rows = conn.execute(query, params).fetchall()
        else:
            query = """
            SELECT link_key, as_of_period, link_type, experimental,
                   driver_growth, follower_growth, divergence_pp,
                   nowcast_direction, nowcast_hit, correlation,
                   verdict, members_json, created_at
            FROM channel_check_log
            WHERE members_json LIKE ?
               OR members_json LIKE ?
            ORDER BY as_of_period DESC, created_at DESC
            LIMIT ?
            """
            rows = conn.execute(query, (quoted_pattern, raw_pattern, limit)).fetchall()

        return [
            ChannelCheckLogRecord(
                link_key=r[0],
                as_of_period=r[1],
                link_type=cast(LinkType, r[2]),
                experimental=bool(r[3]),
                driver_growth=float(r[4]) if r[4] is not None else None,
                follower_growth=float(r[5]) if r[5] is not None else None,
                divergence_pp=float(r[6]) if r[6] is not None else None,
                nowcast_direction=cast(NowcastDirection, r[7])
                if r[7] is not None
                else None,
                nowcast_hit=bool(r[8]) if r[8] is not None else None,
                correlation=float(r[9]) if r[9] is not None else None,
                verdict=cast(ChannelCheckVerdict, r[10]),
                members_json=r[11],
                created_at=r[12] if r[12] else "",
            )
            for r in rows
        ]
    finally:
        conn.close()
