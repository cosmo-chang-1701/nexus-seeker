"""提領跑道快照的存取層（寫入一律經 `database/connection.py`；docs/risk_portfolio/05）。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from database.connection import execute_write_many_async, get_read_connection

_UPSERT_SQL = """
    INSERT OR REPLACE INTO withdrawal_runway_snapshot
        (user_id, as_of, nav, nav_date, zero_years, gfc_years, dotcom_years,
         stress_years, capped, next_withdrawal, boxx_value, boxx_payments,
         beta, beta_is_fallback, cpi_missing, next_date)
    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
"""

_COLUMNS = (
    "user_id, as_of, nav, nav_date, zero_years, gfc_years, dotcom_years, "
    "stress_years, capped, next_withdrawal, boxx_value, boxx_payments, "
    "beta, beta_is_fallback, cpi_missing, next_date"
)


@dataclass(frozen=True)
class RunwaySnapshot:
    user_id: int
    as_of: str  # 計算日 YYYY-MM-DD
    nav: float
    nav_date: str  # 使用的 NAV 快照日
    zero_years: float
    gfc_years: float
    dotcom_years: float
    stress_years: float
    capped: bool
    next_withdrawal: float
    boxx_value: float
    boxx_payments: int
    beta: float
    beta_is_fallback: bool
    cpi_missing: bool
    next_date: Optional[str]

    def as_row(self) -> tuple[object, ...]:
        return (
            self.user_id,
            self.as_of,
            self.nav,
            self.nav_date,
            self.zero_years,
            self.gfc_years,
            self.dotcom_years,
            self.stress_years,
            int(self.capped),
            self.next_withdrawal,
            self.boxx_value,
            self.boxx_payments,
            self.beta,
            int(self.beta_is_fallback),
            int(self.cpi_missing),
            self.next_date,
        )


async def upsert_snapshots(snapshots: list[RunwaySnapshot]) -> None:
    if not snapshots:
        return
    await execute_write_many_async(
        [(_UPSERT_SQL, [s.as_row() for s in snapshots], True)]
    )


def load_snapshot(user_id: int) -> Optional[RunwaySnapshot]:
    """讀取最新快照（同步、無副作用；呼叫端以 asyncio.to_thread 執行）。"""
    conn = get_read_connection()
    try:
        row = conn.execute(
            f"SELECT {_COLUMNS} FROM withdrawal_runway_snapshot WHERE user_id = ?",
            (user_id,),
        ).fetchone()
    finally:
        conn.close()
    if row is None:
        return None
    return RunwaySnapshot(
        user_id=int(row[0]),
        as_of=str(row[1]),
        nav=float(row[2]),
        nav_date=str(row[3]),
        zero_years=float(row[4]),
        gfc_years=float(row[5]),
        dotcom_years=float(row[6]),
        stress_years=float(row[7]),
        capped=bool(row[8]),
        next_withdrawal=float(row[9]),
        boxx_value=float(row[10]),
        boxx_payments=int(row[11]),
        beta=float(row[12]),
        beta_is_fallback=bool(row[13]),
        cpi_missing=bool(row[14]),
        next_date=str(row[15]) if row[15] is not None else None,
    )
