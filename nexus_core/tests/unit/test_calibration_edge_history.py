"""calibration.edge_history：edge 前向蒐集歷史 → micro-report 每日快照的轉換。"""

import json
import math
import sqlite3
import zlib
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import pandas as pd
import pytest

from calibration.edge_history import (
    EdgeHistorySource,
    adv_and_atr_asof,
    build_edge_snapshots,
    daily_close_gex,
    em_rows_by_date,
    support_wall_from_profile,
)
from market_time import get_session_bounds_utc

NY = ZoneInfo("America/New_York")


def _bucket(y: int, mo: int, d: int, h: int, mi: int) -> str:
    """美東時間 → edge 的 bucket_ts ('YYYY-MM-DDTHH:MM:SSZ')。"""
    return (
        datetime(y, mo, d, h, mi, tzinfo=NY)
        .astimezone(timezone.utc)
        .strftime("%Y-%m-%dT%H:%M:%SZ")
    )


def _sessions(start: date, end: date) -> Any:
    return get_session_bounds_utc(start, end)


def test_daily_close_gex_picks_last_in_session_bucket() -> None:
    rows = [
        {"bucket_ts": _bucket(2026, 9, 22, 10, 0), "spot": 1.0},
        {"bucket_ts": _bucket(2026, 9, 22, 15, 45), "spot": 2.0},
        {"bucket_ts": _bucket(2026, 9, 22, 17, 0), "spot": 9.0},  # 盤後
        {"bucket_ts": _bucket(2026, 11, 27, 12, 45), "spot": 3.0},  # 半日市
        {"bucket_ts": _bucket(2026, 11, 27, 13, 15), "spot": 9.0},  # 半日市收盤後
        {"bucket_ts": _bucket(2026, 11, 26, 12, 0), "spot": 9.0},  # 感恩節
        {"bucket_ts": "garbage", "spot": 9.0},
    ]
    out = daily_close_gex(rows, _sessions(date(2026, 9, 21), date(2026, 11, 30)))
    assert {d: r["spot"] for d, r in out.items()} == {
        "2026-09-22": 2.0,
        "2026-11-27": 3.0,
    }


def test_support_wall_from_profile_uses_net_gex_below_spot() -> None:
    profile = {"95": 3e6, "90": 5e6, "105": 9e6, "100": 8e6, "85": -1e6, "x": 1}
    # 105 在現價上方、100 等於現價，都不是支撐牆候選
    assert support_wall_from_profile(profile, 100.0) == (90.0, 5e6)
    assert support_wall_from_profile({}, 100.0) == (0.0, 0.0)


def test_adv_and_atr_asof_has_no_lookahead() -> None:
    idx = pd.bdate_range("2026-08-01", periods=40)
    hist = pd.DataFrame(
        {
            "High": [101.0] * 40,
            "Low": [99.0] * 40,
            "Close": [100.0] * 40,
            "Volume": [1e6] * 39 + [1e9],
        },
        index=idx,
    )
    day_before_spike = idx[-2].strftime("%Y-%m-%d")
    adv, atr = adv_and_atr_asof(hist, day_before_spike)
    assert adv == pytest.approx(1e8)  # 最後一天的爆量不得納入
    assert atr == pytest.approx(2.0)
    assert adv_and_atr_asof(hist, idx[5].strftime("%Y-%m-%d")) == (None, None)


def test_em_rows_by_date_filters_non_trading_days() -> None:
    rows = [
        {
            "trade_date": "2026-09-22",
            "expiry": "2026-09-29",
            "dte": 7,
            "call_mid": 2.0,
            "put_mid": 2.0,
        },
        {
            "trade_date": "2026-09-22",
            "expiry": "2026-09-25",
            "dte": 3,
            "call_mid": 1.0,
            "put_mid": 1.0,
        },
        {
            "trade_date": "2026-09-26",
            "expiry": "2026-10-02",
            "dte": 6,
            "call_mid": 1.0,
            "put_mid": 1.0,
        },
        {
            "trade_date": "2026-09-22",
            "expiry": "bad",
            "dte": 0,
            "call_mid": 1.0,
            "put_mid": 1.0,
        },
    ]
    out = em_rows_by_date(rows, _sessions(date(2026, 9, 21), date(2026, 9, 28)))
    assert list(out) == ["2026-09-22"]
    by_dte = {r["dte"]: r["em_weekly_calendar"] for r in out["2026-09-22"]}
    assert by_dte[7.0] == pytest.approx(4.0 * math.sqrt(math.pi / 2.0))
    assert by_dte[3.0] == pytest.approx(
        2.0 * math.sqrt(math.pi / 2.0) * math.sqrt(7 / 3)
    )


def _make_edge_db(path: Path) -> None:
    """建立與 nexus_edge_scraper/database.py 相同 schema 的測試 DB。"""
    conn = sqlite3.connect(path)
    try:
        conn.executescript(
            """
            CREATE TABLE gex_snapshot_history (
                symbol TEXT NOT NULL, bucket_ts TEXT NOT NULL,
                captured_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                spot REAL, net_gex REAL, call_wall REAL, put_wall REAL,
                gex_profile_z BLOB, PRIMARY KEY (symbol, bucket_ts)
            ) WITHOUT ROWID;
            CREATE TABLE em_snapshot_history (
                symbol TEXT NOT NULL, trade_date TEXT NOT NULL, expiry TEXT NOT NULL,
                dte INTEGER NOT NULL, spot REAL NOT NULL, strike REAL NOT NULL,
                call_mid REAL NOT NULL, put_mid REAL NOT NULL,
                captured_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY (symbol, trade_date, expiry)
            ) WITHOUT ROWID;
            """
        )
        profile = zlib.compress(json.dumps({"95": 2e6, "105": 5e6}).encode("utf-8"))
        conn.executemany(
            "INSERT INTO gex_snapshot_history (symbol, bucket_ts, spot, net_gex, "
            "call_wall, put_wall, gex_profile_z) VALUES (?, ?, ?, ?, ?, ?, ?)",
            [
                (
                    "AAPL",
                    _bucket(2026, 9, 21, 15, 45),
                    100.0,
                    1e7,
                    105.0,
                    95.0,
                    profile,
                ),
                (
                    "AAPL",
                    _bucket(2026, 9, 22, 15, 45),
                    100.0,
                    1e7,
                    105.0,
                    95.0,
                    profile,
                ),
            ],
        )
        conn.execute(
            "INSERT INTO em_snapshot_history (symbol, trade_date, expiry, dte, spot, "
            "strike, call_mid, put_mid) VALUES ('AAPL', '2026-09-22', '2026-09-29', 7, "
            "100.0, 100.0, 2.0, 2.0)"
        )
        conn.commit()
    finally:
        conn.close()


def test_build_edge_snapshots_from_edge_db_file(tmp_path: Path) -> None:
    db = tmp_path / "edge_cache.db"
    _make_edge_db(db)
    source = EdgeHistorySource(db_path=db)
    assert source.list_symbols() == ["AAPL"]

    idx = pd.bdate_range(end="2026-09-22", periods=60)
    hist = pd.DataFrame(
        {
            "High": [101.0] * 60,
            "Low": [99.0] * 60,
            "Close": [100.0] * 60,
            "Volume": [4e6] * 60,
        },
        index=idx,
    )
    requested: list[tuple[str, date]] = []

    def loader(symbol: str, start: date) -> pd.DataFrame:
        requested.append((symbol, start))
        return hist

    snaps = build_edge_snapshots(source, history_loader=loader)
    assert [s["date"] for s in snaps] == ["2026-09-21", "2026-09-22"]
    assert requested and requested[0][1] <= date(2026, 9, 21) - timedelta(days=60)

    s22 = snaps[1]
    assert s22["gex"]["support_strike"] == 95.0
    assert s22["gex"]["support_gex"] == 2e6
    assert s22["adv_dollar_20d"] == pytest.approx(4e8)
    assert [e["dte"] for e in s22["em"]] == [7.0]
    assert snaps[0]["em"] == []


def test_edge_history_source_requires_a_location() -> None:
    with pytest.raises(ValueError):
        EdgeHistorySource()


def test_micro_report_accepts_edge_snapshots(tmp_path: Path, monkeypatch: Any) -> None:
    from calibration import microstructure

    monkeypatch.setattr(microstructure, "label_wall_holds", lambda snaps, h: [])
    snaps = [
        {
            "date": "2026-09-22",
            "symbol": "AAPL",
            "spot": 100.0,
            "adv_dollar_20d": 4e8,
            "atr_1d": 2.0,
            "gex": {"support_strike": 95.0, "support_gex": 2e6},
            "em": [],
        }
    ]
    report = microstructure.build_micro_report(tmp_path, snapshots=snaps)
    assert report["n_snapshots"] == 1
    assert report["n_with_support_wall"] == 1
