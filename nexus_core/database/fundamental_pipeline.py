"""基本面分析管線資料庫持久層。

遵循 Nexus Seeker 資料庫單一寫入器架構（Single-Writer Invariant）：
- 寫入一律經由 `database.connection` 之 `execute_write_async` / `execute_write_many_async`。
- 讀取一律經由 `get_read_connection()` 並在 `try ... finally: conn.close()` 中確實釋放。
"""

from __future__ import annotations

import logging
import re
from collections.abc import Sequence
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
    FairValueRecord,
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
    RevisionScoreRecord,
    WatchCandidateRecord,
    WatchCandidateStatus,
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
    status,
    announced_on
) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
    status = excluded.status,
    announced_on = COALESCE(excluded.announced_on, earnings_surprise.announced_on)
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
    analyst_count,
    fiscal_period
) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
ON CONFLICT(symbol, snapshot_date, horizon, source) DO UPDATE SET
    eps_mean = excluded.eps_mean,
    eps_high = excluded.eps_high,
    eps_low = excluded.eps_low,
    analyst_count = excluded.analyst_count,
    fiscal_period = COALESCE(excluded.fiscal_period, eps_estimate_snapshot.fiscal_period)
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
        surprise.announced_on,
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
            s.announced_on,
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
                   session, eps_basis, status, created_at, announced_on
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
            announced_on=row[14] if row[14] else None,
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
                   session, eps_basis, status, created_at, announced_on
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
            announced_on=row[14] if row[14] else None,
        )
    finally:
        conn.close()


def get_latest_processed_earnings_surprise(
    symbol: str, as_of: str
) -> EarningsSurpriseDTO | None:
    """讀取 as_of（美東 YYYY-MM-DD，含）以前最近一筆 PROCESSED 財務預期差（供 PEAD 判定）。

    只取 status = 'PROCESSED'（PENDING / FAILED 沒有綜合評分）；發布日晚於 as_of 者排除，
    發布日未知（announced_on 為 NULL，例如非事件觸發的寫入）者仍回傳，由呼叫端標註日期不明。
    """
    conn = get_read_connection()
    try:
        row = conn.execute(
            """
            SELECT symbol, fiscal_period, actual_eps, consensus_eps,
                   eps_surprise_pct, actual_revenue, consensus_revenue,
                   revenue_surprise_pct, whisper_eps, composite_score,
                   session, eps_basis, status, created_at, announced_on
            FROM earnings_surprise
            WHERE symbol = ? AND status = 'PROCESSED'
              AND (announced_on IS NULL OR announced_on <= ?)
            ORDER BY fiscal_period DESC, created_at DESC
            LIMIT 1
            """,
            (symbol.upper(), as_of),
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
            announced_on=row[14] if row[14] else None,
        )
    finally:
        conn.close()


def get_pending_earnings_surprise_keys(since_utc: datetime) -> list[tuple[str, str]]:
    """讀取 `created_at` 不早於 since_utc、狀態仍為 PENDING 之 (symbol, fiscal_period)。

    `created_at` 為首次寫入時間（SQLite CURRENT_TIMESTAMP，UTC；upsert 不更新），
    即該財季首次被財報事件寫成 PENDING 的時間，供 PENDING 重試排程界定回看窗口。
    """
    if since_utc.tzinfo is not None:
        since_utc = since_utc.astimezone(timezone.utc)
    since_str = since_utc.strftime("%Y-%m-%d %H:%M:%S")
    conn = get_read_connection()
    try:
        rows = conn.execute(
            """
            SELECT symbol, fiscal_period
            FROM earnings_surprise
            WHERE status = 'PENDING' AND created_at >= ?
            ORDER BY symbol, fiscal_period
            """,
            (since_str,),
        ).fetchall()
        return [(str(r[0]), str(r[1])) for r in rows]
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
            s.fiscal_period,
        )
        for s in snapshots
    ]
    await execute_write_many_async([(_UPSERT_EPS_ESTIMATE_SNAPSHOT_SQL, rows, True)])


def _row_to_snapshot(r: Any) -> EPSEstimateSnapshotRecord:
    return EPSEstimateSnapshotRecord(
        symbol=r[0],
        snapshot_date=r[1],
        horizon=cast(EstimateHorizon, r[2]),
        source=r[3],
        eps_mean=float(r[4]),
        eps_high=float(r[5]) if r[5] is not None else None,
        eps_low=float(r[6]) if r[6] is not None else None,
        analyst_count=int(r[7]) if r[7] is not None else None,
        created_at=r[8] if r[8] else "",
        fiscal_period=r[9] if r[9] else None,
    )


def get_eps_estimate_snapshots(
    symbol: str, snapshot_date: str | None = None, as_of: str | None = None
) -> list[EPSEstimateSnapshotRecord]:
    """讀取特定標的之分析師預估快照。

    - 指定 snapshot_date：只取該日。
    - 未指定：取 as_of（含，YYYY-MM-DD）以前最新的快照日；as_of 為 None 時取全表最新。
    """
    conn = get_read_connection()
    try:
        if snapshot_date is None:
            if as_of is None:
                max_row = conn.execute(
                    "SELECT MAX(snapshot_date) FROM eps_estimate_snapshot WHERE symbol = ?",
                    (symbol.upper(),),
                ).fetchone()
            else:
                max_row = conn.execute(
                    """
                    SELECT MAX(snapshot_date) FROM eps_estimate_snapshot
                    WHERE symbol = ? AND snapshot_date <= ?
                    """,
                    (symbol.upper(), as_of),
                ).fetchone()
            if not max_row or not max_row[0]:
                return []
            target_date = max_row[0]
        else:
            target_date = snapshot_date

        rows = conn.execute(
            """
            SELECT symbol, snapshot_date, horizon, source,
                   eps_mean, eps_high, eps_low, analyst_count, created_at, fiscal_period
            FROM eps_estimate_snapshot
            WHERE symbol = ? AND snapshot_date = ?
            ORDER BY CASE horizon WHEN '0q' THEN 1 WHEN '+1q' THEN 2 WHEN '0y' THEN 3 WHEN '+1y' THEN 4 ELSE 5 END ASC
            """,
            (symbol.upper(), target_date),
        ).fetchall()
        return [_row_to_snapshot(r) for r in rows]
    finally:
        conn.close()


def get_prior_eps_estimate_snapshots(
    symbol: str, window_start: str, window_end: str
) -> list[EPSEstimateSnapshotRecord]:
    """讀取 [window_start, window_end]（含兩端，YYYY-MM-DD）內所有快照，依快照日新到舊。

    供修正動能取 t-30d 基準：呼叫端以財期配對並挑選最接近 t-30 的快照日，
    窗口內沒有任何快照時回傳空清單（不退回窗口外的舊快照）。
    """
    conn = get_read_connection()
    try:
        rows = conn.execute(
            """
            SELECT symbol, snapshot_date, horizon, source,
                   eps_mean, eps_high, eps_low, analyst_count, created_at, fiscal_period
            FROM eps_estimate_snapshot
            WHERE symbol = ? AND snapshot_date >= ? AND snapshot_date <= ?
            ORDER BY snapshot_date DESC, CASE horizon WHEN '0q' THEN 1 WHEN '+1q' THEN 2 WHEN '0y' THEN 3 WHEN '+1y' THEN 4 ELSE 5 END ASC
            """,
            (symbol.upper(), window_start, window_end),
        ).fetchall()
        return [_row_to_snapshot(r) for r in rows]
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


def get_prior_guidance_extraction(
    symbol: str,
    before_period: str,
    exclude_accession: str | None = None,
) -> GuidanceExtractionDTO | None:
    """讀取早於指定財季（`YYYY-Qn` 字串序）之最近一筆管理層指引記錄。

    僅考慮已正規化為 `YYYY-Qn` 之期別，並排除同一申報（source_accession），
    避免同季重送或更新季度被誤當成「前期」。
    """
    conn = get_read_connection()
    try:
        row = conn.execute(
            """
            SELECT symbol, fiscal_period, source_accession, model_version,
                   confidence_score, tone_delta_score, data_json, created_at
            FROM guidance_extraction
            WHERE symbol = ?
              AND fiscal_period GLOB '[0-9][0-9][0-9][0-9]-Q[1-4]'
              AND fiscal_period < ?
              AND source_accession != ?
            ORDER BY fiscal_period DESC, created_at DESC
            LIMIT 1
            """,
            (symbol.upper(), before_period, exclude_accession or ""),
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
    members_json = excluded.members_json,
    created_at = CURRENT_TIMESTAMP
WHERE excluded.verdict != 'INSUFFICIENT'
   OR channel_check_log.verdict = 'INSUFFICIENT'
"""

# v093 的 CHECK(nowcast_direction IN (..., NULL)) 在 SQLite 中等同無約束（IN 清單含 NULL
# 時，不合法值的比較結果為 NULL 而非 false）。v093 已在正式 DB 執行且不新增遷移，
# 改於寫入端在 Python 層驗證列舉值與期別格式。
_VALID_LINK_TYPES: frozenset[str] = frozenset({"CAUSAL", "NOWCAST"})
_VALID_VERDICTS: frozenset[str] = frozenset({"CONFIRM", "DIVERGE", "INSUFFICIENT"})
_VALID_NOWCAST_DIRECTIONS: frozenset[str] = frozenset(
    {"NOWCAST_UP", "NOWCAST_DOWN", "FLAT"}
)


def _validate_channel_check_record(record: ChannelCheckLogRecord) -> None:
    """寫入前驗證列舉值與期別格式；不合法時拋出 ValueError（整批不寫入）。"""
    from market_analysis.fundamental_pipeline.alt_data_metrics import validate_period

    validate_period(record.as_of_period)
    if record.link_type not in _VALID_LINK_TYPES:
        raise ValueError(f"channel_check_log.link_type 不合法: {record.link_type!r}")
    if record.verdict not in _VALID_VERDICTS:
        raise ValueError(f"channel_check_log.verdict 不合法: {record.verdict!r}")
    if (
        record.nowcast_direction is not None
        and record.nowcast_direction not in _VALID_NOWCAST_DIRECTIONS
    ):
        raise ValueError(
            f"channel_check_log.nowcast_direction 不合法: {record.nowcast_direction!r}"
        )


async def save_channel_check_log(record: ChannelCheckLogRecord) -> None:
    """寫入或更新單筆產業鏈交叉驗證日誌（INSUFFICIENT 不覆蓋同期既有的非 INSUFFICIENT 結果）。"""
    _validate_channel_check_record(record)
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
    """批次寫入產業鏈交叉驗證日誌（分塊寫入；INSUFFICIENT 不覆蓋同期既有的非 INSUFFICIENT 結果）。"""
    if not records:
        return
    for r in records:
        _validate_channel_check_record(r)
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

    if not link_keys:
        return []

    conn = get_read_connection()
    try:
        # 只依靜態對照表的 link_key 精確比對；不再對 members_json 做 LIKE 模糊比對
        # （短代碼如 "F"、"PL" 會配到所有列）。
        placeholders = ",".join("?" for _ in link_keys)
        query = f"""
        SELECT link_key, as_of_period, link_type, experimental,
               driver_growth, follower_growth, divergence_pp,
               nowcast_direction, nowcast_hit, correlation,
               verdict, members_json, created_at
        FROM channel_check_log
        WHERE link_key IN ({placeholders})
        ORDER BY as_of_period DESC, created_at DESC
        LIMIT ?
        """
        params: list[Any] = [*link_keys, limit]
        # nosemgrep: python.lang.security.audit.formatted-sql-query.formatted-sql-query, python.sqlalchemy.security.sqlalchemy-execute-raw-query.sqlalchemy-execute-raw-query
        rows = conn.execute(query, params).fetchall()

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


# ============================================================================
# PR5 分析師修正動能、公允價值與次日基本面候選名單持久層
# ============================================================================

_UPSERT_REVISION_SCORE_SQL = """
INSERT INTO revision_score_log (
    symbol,
    trading_date,
    score_30d,
    breadth_ratio,
    is_pead_aligned,
    detail_json
) VALUES (?, ?, ?, ?, ?, ?)
ON CONFLICT(symbol, trading_date) DO UPDATE SET
    score_30d = excluded.score_30d,
    breadth_ratio = excluded.breadth_ratio,
    is_pead_aligned = excluded.is_pead_aligned,
    detail_json = excluded.detail_json
"""

_UPSERT_FAIR_VALUE_SQL = """
INSERT INTO fair_value_log (
    symbol,
    trading_date,
    dcf_value,
    comps_value,
    fair_value,
    margin_of_safety,
    discount_rate,
    equity_risk_premium,
    flags_json,
    method,
    spot_price
) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
ON CONFLICT(symbol, trading_date) DO UPDATE SET
    dcf_value = excluded.dcf_value,
    comps_value = excluded.comps_value,
    fair_value = excluded.fair_value,
    margin_of_safety = excluded.margin_of_safety,
    method = excluded.method,
    spot_price = excluded.spot_price,
    discount_rate = excluded.discount_rate,
    equity_risk_premium = excluded.equity_risk_premium,
    flags_json = excluded.flags_json
"""

_UPSERT_WATCH_CANDIDATE_SQL = """
INSERT INTO fundamental_watch_candidate (
    trading_date,
    symbol,
    rank,
    status,
    reasons_json,
    excluded_reason
) VALUES (?, ?, ?, ?, ?, ?)
ON CONFLICT(trading_date, symbol) DO UPDATE SET
    rank = excluded.rank,
    status = excluded.status,
    reasons_json = excluded.reasons_json,
    excluded_reason = excluded.excluded_reason
"""


async def save_revision_score(record: RevisionScoreRecord) -> None:
    """寫入或更新單筆分析師修正動能分數。"""
    params = (
        record.symbol.upper(),
        record.trading_date,
        record.score_30d,
        record.breadth_ratio,
        1 if record.is_pead_aligned else 0,
        record.detail_json,
    )
    await execute_write_async(_UPSERT_REVISION_SCORE_SQL, params)


async def save_revision_scores(records: Sequence[RevisionScoreRecord]) -> None:
    """批次寫入或更新分析師修正動能分數。"""
    if not records:
        return
    params_list = [
        (
            r.symbol.upper(),
            r.trading_date,
            r.score_30d,
            r.breadth_ratio,
            1 if r.is_pead_aligned else 0,
            r.detail_json,
        )
        for r in records
    ]
    await execute_write_many_async([(_UPSERT_REVISION_SCORE_SQL, params_list, True)])


def get_revision_score(symbol: str, trading_date: str) -> RevisionScoreRecord | None:
    """讀取特定標的與交易日之修正動能記錄。"""
    conn = get_read_connection()
    try:
        row = conn.execute(
            """
            SELECT symbol, trading_date, score_30d, breadth_ratio,
                   is_pead_aligned, detail_json, created_at
            FROM revision_score_log
            WHERE symbol = ? AND trading_date = ?
            """,
            (symbol.upper(), trading_date),
        ).fetchone()
        if not row:
            return None
        return RevisionScoreRecord(
            symbol=row[0],
            trading_date=row[1],
            score_30d=float(row[2]) if row[2] is not None else None,
            breadth_ratio=float(row[3]) if row[3] is not None else None,
            is_pead_aligned=bool(row[4]),
            detail_json=row[5],
            created_at=row[6] if row[6] else "",
        )
    finally:
        conn.close()


def get_latest_revision_score(symbol: str) -> RevisionScoreRecord | None:
    """讀取特定標的最新一筆修正動能記錄。"""
    conn = get_read_connection()
    try:
        row = conn.execute(
            """
            SELECT symbol, trading_date, score_30d, breadth_ratio,
                   is_pead_aligned, detail_json, created_at
            FROM revision_score_log
            WHERE symbol = ?
            ORDER BY trading_date DESC, created_at DESC
            LIMIT 1
            """,
            (symbol.upper(),),
        ).fetchone()
        if not row:
            return None
        return RevisionScoreRecord(
            symbol=row[0],
            trading_date=row[1],
            score_30d=float(row[2]) if row[2] is not None else None,
            breadth_ratio=float(row[3]) if row[3] is not None else None,
            is_pead_aligned=bool(row[4]),
            detail_json=row[5],
            created_at=row[6] if row[6] else "",
        )
    finally:
        conn.close()


async def save_fair_value(record: FairValueRecord) -> None:
    """寫入或更新單筆公允價值與安全邊際記錄。"""
    params = (
        record.symbol.upper(),
        record.trading_date,
        record.dcf_value,
        record.comps_value,
        record.fair_value,
        record.margin_of_safety,
        record.discount_rate,
        record.equity_risk_premium,
        record.flags_json,
        record.method,
        record.spot_price,
    )
    await execute_write_async(_UPSERT_FAIR_VALUE_SQL, params)


async def save_fair_values(records: Sequence[FairValueRecord]) -> None:
    """批次寫入或更新公允價值記錄。"""
    if not records:
        return
    params_list = [
        (
            r.symbol.upper(),
            r.trading_date,
            r.dcf_value,
            r.comps_value,
            r.fair_value,
            r.margin_of_safety,
            r.discount_rate,
            r.equity_risk_premium,
            r.flags_json,
            r.method,
            r.spot_price,
        )
        for r in records
    ]
    await execute_write_many_async([(_UPSERT_FAIR_VALUE_SQL, params_list, True)])


def get_fair_value(symbol: str, trading_date: str) -> FairValueRecord | None:
    """讀取特定標的與交易日之公允價值記錄。"""
    conn = get_read_connection()
    try:
        row = conn.execute(
            """
            SELECT symbol, trading_date, dcf_value, comps_value, fair_value,
                   margin_of_safety, discount_rate, equity_risk_premium,
                   flags_json, created_at, method, spot_price
            FROM fair_value_log
            WHERE symbol = ? AND trading_date = ?
            """,
            (symbol.upper(), trading_date),
        ).fetchone()
        if not row:
            return None
        return FairValueRecord(
            symbol=row[0],
            trading_date=row[1],
            dcf_value=float(row[2]) if row[2] is not None else None,
            comps_value=float(row[3]) if row[3] is not None else None,
            fair_value=float(row[4]) if row[4] is not None else None,
            margin_of_safety=float(row[5]) if row[5] is not None else None,
            discount_rate=float(row[6]),
            equity_risk_premium=float(row[7]),
            flags_json=row[8],
            method=str(row[10]) if row[10] else "NONE",
            spot_price=float(row[11]) if row[11] is not None else None,
            created_at=row[9] if row[9] else "",
        )
    finally:
        conn.close()


def get_latest_fair_value(symbol: str) -> FairValueRecord | None:
    """讀取特定標的最新一筆公允價值記錄。"""
    conn = get_read_connection()
    try:
        row = conn.execute(
            """
            SELECT symbol, trading_date, dcf_value, comps_value, fair_value,
                   margin_of_safety, discount_rate, equity_risk_premium,
                   flags_json, created_at, method, spot_price
            FROM fair_value_log
            WHERE symbol = ?
            ORDER BY trading_date DESC, created_at DESC
            LIMIT 1
            """,
            (symbol.upper(),),
        ).fetchone()
        if not row:
            return None
        return FairValueRecord(
            symbol=row[0],
            trading_date=row[1],
            dcf_value=float(row[2]) if row[2] is not None else None,
            comps_value=float(row[3]) if row[3] is not None else None,
            fair_value=float(row[4]) if row[4] is not None else None,
            margin_of_safety=float(row[5]) if row[5] is not None else None,
            discount_rate=float(row[6]),
            equity_risk_premium=float(row[7]),
            flags_json=row[8],
            method=str(row[10]) if row[10] else "NONE",
            spot_price=float(row[11]) if row[11] is not None else None,
            created_at=row[9] if row[9] else "",
        )
    finally:
        conn.close()


async def save_watch_candidates(candidates: Sequence[WatchCandidateRecord]) -> None:
    """批次寫入或更新基本面次日候選名單。"""
    if not candidates:
        return
    params_list = [
        (
            c.trading_date,
            c.symbol.upper(),
            c.rank,
            c.status,
            c.reasons_json,
            c.excluded_reason,
        )
        for c in candidates
    ]
    await execute_write_many_async([(_UPSERT_WATCH_CANDIDATE_SQL, params_list, True)])


def get_watch_candidates(
    trading_date: str | None = None, status: str | None = None
) -> list[WatchCandidateRecord]:
    """查詢次日基本面候選名單。未指定 trading_date 時取最新交易日。"""
    conn = get_read_connection()
    try:
        if trading_date is None:
            max_row = conn.execute(
                "SELECT MAX(trading_date) FROM fundamental_watch_candidate"
            ).fetchone()
            if not max_row or not max_row[0]:
                empty_candidates: list[WatchCandidateRecord] = []
                return empty_candidates
            target_date = max_row[0]
        else:
            target_date = trading_date

        if status is not None:
            rows = conn.execute(
                """
                SELECT trading_date, symbol, rank, status,
                       reasons_json, excluded_reason, created_at
                FROM fundamental_watch_candidate
                WHERE trading_date = ? AND status = ?
                ORDER BY rank ASC
                """,
                (target_date, status),
            ).fetchall()
        else:
            rows = conn.execute(
                """
                SELECT trading_date, symbol, rank, status,
                       reasons_json, excluded_reason, created_at
                FROM fundamental_watch_candidate
                WHERE trading_date = ?
                ORDER BY rank ASC
                """,
                (target_date,),
            ).fetchall()

        return [
            WatchCandidateRecord(
                trading_date=r[0],
                symbol=r[1],
                rank=int(r[2]),
                status=cast(WatchCandidateStatus, r[3]),
                reasons_json=r[4],
                excluded_reason=r[5],
                created_at=r[6] if r[6] else "",
            )
            for r in rows
        ]
    finally:
        conn.close()


def get_latest_watch_candidates(limit: int = 10) -> list[WatchCandidateRecord]:
    """讀取最新交易日前 N 名基本面候選名單。"""
    conn = get_read_connection()
    try:
        max_row = conn.execute(
            "SELECT MAX(trading_date) FROM fundamental_watch_candidate"
        ).fetchone()
        if not max_row or not max_row[0]:
            empty_list: list[WatchCandidateRecord] = []
            return empty_list
        target_date = max_row[0]

        rows = conn.execute(
            """
            SELECT trading_date, symbol, rank, status,
                   reasons_json, excluded_reason, created_at
            FROM fundamental_watch_candidate
            WHERE trading_date = ?
            ORDER BY rank ASC
            LIMIT ?
            """,
            (target_date, limit),
        ).fetchall()

        return [
            WatchCandidateRecord(
                trading_date=r[0],
                symbol=r[1],
                rank=int(r[2]),
                status=cast(WatchCandidateStatus, r[3]),
                reasons_json=r[4],
                excluded_reason=r[5],
                created_at=r[6] if r[6] else "",
            )
            for r in rows
        ]
    finally:
        conn.close()
