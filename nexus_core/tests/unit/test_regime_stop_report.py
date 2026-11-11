"""regime-stop-report：體制停損 κ 離線比較（純函式，無網路）。"""

import json
import sqlite3
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pandas as pd

from calibration import regime_stop_report as rs

CLOSE = datetime(2026, 10, 1, 20, 0, tzinfo=timezone.utc)


def _make_db(path: Path) -> None:
    conn = sqlite3.connect(path)
    conn.execute(
        "CREATE TABLE sec_filing_event (symbol TEXT, form TEXT, items TEXT, accepted_at TEXT)"
    )
    conn.execute("CREATE TABLE economic_calendar_events (event TEXT, event_time TEXT)")
    conn.executemany(
        "INSERT INTO sec_filing_event VALUES (?,?,?,?)",
        [
            ("nvda", "8-K", "2.02,9.01", "2026-10-02T09:04:26-04:00"),
            ("NVDA", "8-K", "5.02", "2026-10-03T09:00:00-04:00"),
            ("AMD", "10-Q", "2.02", "2026-10-03T09:00:00-04:00"),
        ],
    )
    conn.executemany(
        "INSERT INTO economic_calendar_events VALUES (?,?)",
        [
            ("CPI 年增率", "2026-05-12 12:30:00"),
            ("非農就業人數", "2026-10-28T18:00:00Z"),
            ("FOMC Minutes", "2026-10-29T18:00:00Z"),
            ("CPI Revision", "2026-10-30T18:00:00Z"),
            ("Retail Sales", "2026-10-31T18:00:00Z"),
            ("聯準會利率決策", "2026-11-05 19:00:00"),
        ],
    )
    conn.commit()
    conn.close()


def test_load_event_times(tmp_path: Path) -> None:
    db = tmp_path / "s.db"
    _make_db(db)
    earnings, macro = rs.load_event_times(db)
    assert list(earnings) == ["NVDA"]
    assert earnings["NVDA"] == [datetime(2026, 10, 2, 13, 4, 26, tzinfo=timezone.utc)]
    assert len(macro) == 3
    assert datetime(2026, 10, 28, 18, 0, tzinfo=timezone.utc) in macro


def test_load_event_times_missing_tables(tmp_path: Path) -> None:
    db = tmp_path / "empty.db"
    sqlite3.connect(db).close()
    assert rs.load_event_times(db) == ({}, [])


def test_hours_to_next_event_window_edges() -> None:
    def ev(h: float) -> list[datetime]:
        return [CLOSE + timedelta(hours=h)]

    assert rs.hours_to_next_event(CLOSE, ev(0), []) is None
    assert rs.hours_to_next_event(CLOSE, ev(48), []) == 48.0
    assert rs.hours_to_next_event(CLOSE, ev(48.01), []) is None
    assert rs.hours_to_next_event(CLOSE, ev(-1), []) is None
    assert rs.hours_to_next_event(CLOSE, ev(30), ev(10)) == 10.0


def _gex(**kw: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "support_strike": 95.0,
        "support_gex": 1.0,
        "put_wall": 95.0,
        "call_wall": 110.0,
        "net_gex": 5.0,
        "net_profile": {"95": 2.0, "110": -1.0},
    }
    base.update(kw)
    return base


def test_regime_branches() -> None:
    assert rs.classify_regime(_gex(), 10.0) == ("EVENT_RISK", None)
    assert rs.classify_regime({}, None) == ("UNKNOWN", None)
    assert rs.classify_regime({"net_gex": None}, None)[0] == "UNKNOWN"
    assert rs.classify_regime(_gex(net_gex=0.0), None) == (
        "SHORT_GAMMA",
        rs.REASON_NET_GEX,
    )
    assert rs.classify_regime(_gex(net_profile={"95": -2.0}), None) == (
        "SHORT_GAMMA",
        rs.REASON_PUTWALL,
    )
    assert rs.classify_regime(_gex(), None) == ("LONG_GAMMA", None)


def test_anchor_exclusions() -> None:
    assert rs.resolve_anchor(_gex(), 100.0) == (95.0, None)
    assert rs.resolve_anchor(
        _gex(support_strike=0.0, put_wall=0.0, call_wall=0.0), 100.0
    ) == (None, "無結構錨點")
    assert rs.resolve_anchor(_gex(support_strike=105.0), 100.0)[0] is None


def _df(rows: list[tuple[str, float, float, float]]) -> pd.DataFrame:
    return pd.DataFrame(
        [{"Open": o, "Low": lo, "Close": c} for _, o, lo, c in rows],
        index=pd.to_datetime([r[0] for r in rows]),
    )


def test_extract_path_no_lookahead_and_incomplete() -> None:
    df = _df(
        [
            ("2026-10-01", 1, 1, 1),
            ("2026-10-02", 100, 99, 101),
            ("2026-10-05", 101, 100, 102),
        ]
    )
    p = rs.extract_path(df, "2026-10-01", 2)
    assert p == [(100.0, 99.0, 101.0), (101.0, 100.0, 102.0)]
    assert rs.extract_path(df, "2026-10-01", 3) is None
    assert rs.extract_path(None, "2026-10-01", 1) is None
    df.loc[pd.Timestamp("2026-10-02"), "Low"] = float("nan")
    assert rs.extract_path(df, "2026-10-01", 2) is None


def test_simulate_stop() -> None:
    gap = [(90.0, 88.0, 95.0), (96.0, 95.0, 97.0)]
    r0, t0 = rs.simulate_stop(gap, 100.0, 92.0)
    assert t0 and abs(r0 + 0.10) < 1e-12
    intraday = [(100.0, 91.0, 98.0)]
    r, t = rs.simulate_stop(intraday, 100.0, 92.0)
    assert t and abs(r + 0.08) < 1e-12
    r, t = rs.simulate_stop([(100.0, 95.0, 103.0)], 100.0, 92.0)
    assert not t and abs(r - 0.03) < 1e-12


def _snaps(n: int, **gex_kw: Any) -> list[dict[str, Any]]:
    d0 = date(2026, 9, 1)
    return [
        {
            "date": (d0 + timedelta(days=i)).isoformat(),
            "symbol": "AAA",
            "spot": 100.0,
            "atr_1d": 5.0,
            "gex": _gex(**gex_kw),
        }
        for i in range(n)
    ]


def _loader(symbol: str, start: date) -> pd.DataFrame:
    rows = [
        ((date(2026, 8, 31) + timedelta(days=i)).isoformat(), 100.0, 94.0, 101.0)
        for i in range(60)
    ]
    return _df(rows)


def _sessions(snaps: list[dict[str, Any]]) -> dict[str, tuple[str, str]]:
    return {
        s["date"]: (f"{s['date']} 13:30:00", f"{s['date']} 20:00:00") for s in snaps
    }


def test_report_shape_and_json() -> None:
    snaps = _snaps(5) + [
        {
            "date": "2026-09-01",
            "symbol": "BBB",
            "spot": 100.0,
            "atr_1d": 5.0,
            "gex": {},
        },
        {
            "date": "2026-09-02",
            "symbol": "AAA",
            "spot": 100.0,
            "atr_1d": 0.0,
            "gex": _gex(),
        },
    ]
    rep = rs.build_regime_stop_report(snaps, {}, [], _loader, sessions=_sessions(snaps))
    json.dumps(rep, ensure_ascii=False, allow_nan=False)
    assert rep["件數"]["UNKNOWN(無GEX)"] == 1
    assert rep["件數"]["排除:ATR1D或現價無效"] == 1
    assert any("SPY" in x for x in rep["備註"])
    h5 = rep["各觀察期"]["5日"]
    assert h5["已標註"] == 5
    lg = h5["分組"]["LONG_GAMMA"]
    assert lg["判讀"] == rs.INSUFFICIENT
    assert set(lg["各κ"]) == {"0", "0.4", "0.5", "1", "1.75", "3"}
    assert lg["各κ"]["0.5"]["觸發率"]["n"] == 5
    assert "判讀" not in h5["分組"]["全部"]
    assert h5["分組"]["SHORT_GAMMA"]["n"] == 0


def test_report_event_risk_and_groups() -> None:
    snaps = _snaps(3, net_gex=-1.0)
    sess = _sessions(snaps)
    ev = {"AAA": [datetime(2026, 9, 2, 12, 0, tzinfo=timezone.utc)]}
    rep = rs.build_regime_stop_report(snaps, ev, [], _loader, sessions=sess)
    g = rep["各觀察期"]["5日"]["分組"]
    assert g["EVENT_RISK"]["n"] == 1  # 僅 9/1 收盤後 48h 內有財報（9/2 事件在收盤前）
    assert g[rs.GROUP_NET]["n"] == 2
    assert rep["各觀察期"]["10日"]["已標註"] == 3


def test_verdict_rules() -> None:
    base = {"n": 40, "n_dates": 25}
    assert rs._verdict({**base, "ci_pct": [0.1, 1.0]}).startswith("規格κ優於現行")
    assert rs._verdict({**base, "ci_pct": [-1.0, -0.1]}) == "規格κ劣於現行"
    assert rs._verdict({**base, "ci_pct": [-1.0, 1.0]}) == "無顯著差異，維持現行"
    assert rs._verdict({"n": 40, "n_dates": 5, "ci_pct": [1, 2]}) == rs.INSUFFICIENT


def test_cli_requires_snapshot_db(tmp_path: Path, capsys: Any) -> None:
    from calibration.__main__ import main

    rc = main(
        [
            "regime-stop-report",
            "--force",
            "--out",
            str(tmp_path / "out"),
            "--cache-dir",
            str(tmp_path / "cache"),
        ]
    )
    assert rc == 2
    assert "--snapshot-db" in capsys.readouterr().err
