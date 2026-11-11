"""advisor-ytd-report：報告列與限制段落、財報日抓取／快取、snapshot 唯讀整合與 --offline 退出碼
（合成資料，不連網）。"""

from __future__ import annotations

import asyncio
import json
import sqlite3
from datetime import date
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd
import pytest

from calibration import __main__ as cli
from calibration.advisor_ytd_report import (
    ROW_80,
    ROW_BASE,
    ROW_BOXX,
    ROW_D,
    ROW_EARN_OPT,
    ROW_EW,
    ROW_INVESTED,
    ROW_NO_CC,
    ROW_NO_OPT,
    ROW_QQQ,
    ROW_VOO,
    build_advisor_ytd_report,
    fetch_earnings,
    iv_validation,
    load_account_nav,
    load_earnings,
    load_real_iv,
    missing_daily_cache,
    save_earnings,
)
from calibration.data_store import DataStore

DAYS = pd.bdate_range("2024-06-03", periods=420)
UNI = ("AAA", "BBB")
START = DAYS[300].strftime("%Y-%m-%d")


def _bars(prices: np.ndarray) -> pd.DataFrame:
    opens = np.concatenate([[prices[0]], prices[:-1]])
    return pd.DataFrame(
        {
            "Open": opens,
            "High": np.maximum(opens, prices),
            "Low": np.minimum(opens, prices),
            "Close": prices,
            "Volume": np.full(len(prices), 1e6),
        },
        index=DAYS,
    )


def _walk(seed: int, drift: float, vol: float, p0: float) -> np.ndarray:
    rng = np.random.default_rng(seed)
    return p0 * np.exp(np.cumsum(drift + vol * rng.standard_normal(len(DAYS))))


def _store(tmp_path: Path) -> DataStore:
    store = DataStore(tmp_path)
    series = {
        "AAA": _walk(1, 0.001, 0.015, 100.0),
        "BBB": _walk(2, 0.0005, 0.02, 60.0),
        "SPY": _walk(3, 0.0006, 0.008, 400.0),
        "QQQ": _walk(4, 0.0008, 0.011, 300.0),
        "^VIX": 18.0 + 4.0 * np.sin(np.arange(len(DAYS)) / 15.0),
    }
    for sym, px in series.items():
        store.save("1d", sym, _bars(px))
    return store


def test_report_has_every_row_and_limitations(tmp_path: Path) -> None:
    store = _store(tmp_path)
    save_earnings(tmp_path, "AAA", [DAYS[330].date()])
    result, md = build_advisor_ytd_report(store, ytd_start=START, universe=UNI)
    names = [r["name"] for r in result["rows"]]
    assert names == [
        ROW_BASE,
        ROW_INVESTED,
        ROW_EARN_OPT,
        ROW_NO_OPT,
        ROW_NO_CC,
        ROW_D,
        ROW_80,
        ROW_VOO,
        ROW_QQQ,
        ROW_BOXX,
        ROW_EW,
    ]
    for name in names:
        assert f"| {name} |" in md
    for needle in (
        "機械化解讀與限制",
        "GEX／Put Wall／PCR／Max Pain",
        "CPI／NFP／PCE",
        "基本面",
        "小數口數",
        "Black-Scholes",
        "沒有出入金",
        "單一年度",
        "存活者偏差",
        "財報日缺漏",
    ):
        assert needle in md
    assert result["missing_earnings"] == ["BBB"]
    assert "BBB" in md.split("財報日缺漏", 1)[1]
    d = next(r for r in result["rows"] if r["name"] == ROW_D)
    assert d["excess_vs_d"] == pytest.approx(0.0)
    voo = next(r for r in result["rows"] if r["name"] == ROW_VOO)
    spy = store.load("1d", "SPY")
    assert spy is not None
    spy_ret = float(spy["Close"].iloc[-1] / spy["Close"].iloc[300] - 1.0)
    assert voo["total_return"] == pytest.approx(spy_ret, rel=1e-3)  # VOO 以 SPY 串接
    for counters in result["counters"].values():
        assert counters["shorts"] == 0
    assert all(v >= -1e-6 for v in result["min_cash"].values())
    json.dumps(result, default=str)  # 可序列化


def test_ytd_end_limits_period(tmp_path: Path) -> None:
    store = _store(tmp_path)
    end = DAYS[350].strftime("%Y-%m-%d")
    result, _ = build_advisor_ytd_report(
        store, ytd_start=START, ytd_end=end, universe=UNI
    )
    assert result["period"]["end"] == end
    assert result["period"]["trading_days"] == 51


def test_report_without_vix_or_spy_raises_value_error(tmp_path: Path) -> None:
    with pytest.raises(ValueError):
        build_advisor_ytd_report(DataStore(tmp_path), universe=UNI)


def test_missing_daily_cache_lists_required(tmp_path: Path) -> None:
    store = DataStore(tmp_path)
    assert set(missing_daily_cache(store, UNI)) == {"AAA", "BBB", "SPY", "QQQ", "^VIX"}
    full = _store(tmp_path / "full")
    assert missing_daily_cache(full, UNI) == []


# ---------------------------------------------------------------------------
# 財報日
# ---------------------------------------------------------------------------


def test_fetch_earnings_caches_and_swallows_per_symbol_errors(tmp_path: Path) -> None:
    calls: list[str] = []

    def fake(sym: str) -> list[date]:
        calls.append(sym)
        if sym == "BBB":
            raise RuntimeError("yahoo 失敗")
        return [date(2026, 2, 25), date(2026, 5, 27)]

    sleeps: list[float] = []

    async def no_sleep(sec: float) -> None:
        sleeps.append(sec)

    counts = asyncio.run(
        fetch_earnings(["AAA", "BBB", "CCC"], tmp_path, fake, no_sleep)
    )
    assert counts == {"AAA": 2, "CCC": 2}
    assert calls == ["AAA", "BBB", "CCC"]
    assert len(sleeps) == 2 and all(1.5 <= s <= 3.0 for s in sleeps)
    raw = json.loads((tmp_path / "earnings" / "AAA.json").read_text(encoding="utf-8"))
    assert raw["symbol"] == "AAA"
    assert raw["dates"] == ["2026-02-25", "2026-05-27"]
    assert "fetched_at" in raw
    assert not (tmp_path / "earnings" / "BBB.json").exists()
    got, missing = load_earnings(tmp_path, ["AAA", "BBB", "CCC"])
    assert got["AAA"] == [date(2026, 2, 25), date(2026, 5, 27)]
    assert missing == ["BBB"]


def test_load_earnings_treats_empty_and_corrupt_as_missing(tmp_path: Path) -> None:
    save_earnings(tmp_path, "AAA", [])
    (tmp_path / "earnings" / "BBB.json").write_text("{not json", encoding="utf-8")
    got, missing = load_earnings(tmp_path, ["AAA", "BBB"])
    assert got == {} and missing == ["AAA", "BBB"]


# ---------------------------------------------------------------------------
# snapshot.db（唯讀）
# ---------------------------------------------------------------------------


def _snapshot(path: Path) -> None:
    conn = sqlite3.connect(path)
    conn.execute(
        "CREATE TABLE portfolio_nav_daily (user_id INTEGER, date TEXT, nav REAL, "
        "positions_json TEXT, created_at TEXT)"
    )
    conn.execute("CREATE TABLE historical_iv (symbol TEXT, iv REAL, date TEXT)")
    days = DAYS[-12:]
    for k, d in enumerate(days):
        conn.execute(
            "INSERT INTO portfolio_nav_daily VALUES (1, ?, ?, '{}', 'x')",
            (d.strftime("%Y-%m-%d"), 100_000.0 * (1.0 + 0.002 * k)),
        )
        conn.execute(
            "INSERT INTO historical_iv VALUES ('AAA', ?, ?)",
            (0.30 + 0.01 * (k % 3), d.strftime("%Y-%m-%d")),
        )
        conn.execute(
            "INSERT INTO historical_iv VALUES ('SPY', ?, ?)",
            (0.18 + 0.005 * k, d.strftime("%Y-%m-%d")),
        )
    conn.commit()
    conn.close()


def test_snapshot_sections_and_read_only(tmp_path: Path) -> None:
    store = _store(tmp_path)
    db = tmp_path / "snapshot.db"
    _snapshot(db)
    before = db.read_bytes()
    result, md = build_advisor_ytd_report(
        store, ytd_start=START, universe=UNI, snapshot_db=db
    )
    assert db.read_bytes() == before  # 未被改動
    assert "帳戶實際 NAV" in md
    rows = result["same_window"]["rows"]
    assert rows[0]["name"] == "帳戶實際 NAV"
    assert rows[0]["total_return"] == pytest.approx(0.022)
    assert {r["name"] for r in rows} >= {ROW_BASE, ROW_D, ROW_VOO}
    iv = result["iv_validation"]
    assert iv["n"] == 24
    assert iv["median_ratio"] is not None and iv["mae_vol_points"] is not None
    assert "proxy/real" in md
    # 連線為唯讀：寫入應失敗
    conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    with pytest.raises(sqlite3.OperationalError):
        conn.execute("INSERT INTO historical_iv VALUES ('X', 0.1, '2026-01-01')")
    conn.close()
    assert len(load_account_nav(db)) == 12
    assert len(load_real_iv(db)) == 24


def test_snapshot_missing_tables_degrades(tmp_path: Path) -> None:
    store = _store(tmp_path)
    db = tmp_path / "empty.db"
    sqlite3.connect(db).close()
    result, md = build_advisor_ytd_report(
        store, ytd_start=START, universe=UNI, snapshot_db=db
    )
    assert "status" in result["same_window"]
    assert "status" in result["iv_validation"]
    assert "機械化解讀與限制" in md


def test_iv_validation_statistics() -> None:
    idx = pd.DatetimeIndex(pd.bdate_range("2026-10-01", periods=5))
    proxy = pd.DataFrame({"AAA": [0.30, 0.32, 0.28, 0.35, 0.31]}, index=idx)
    real = pd.DataFrame(
        {"symbol": "AAA", "iv": [0.30, 0.30, 0.30, 0.30, 0.30], "date": idx}
    )
    v = iv_validation(proxy, real)
    assert v["n"] == 5
    assert v["median_ratio"] == pytest.approx(1.0333, abs=1e-3)
    assert v["mae_vol_points"] == pytest.approx(2.0)
    assert v["pearson"] is None  # 真實值無變異
    assert iv_validation(proxy, real.iloc[0:0])["n"] == 0


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def test_offline_with_empty_cache_exits_2_with_chinese_message(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    rc = cli.main(
        [
            "advisor-ytd-report",
            "--offline",
            "--force",
            "--cache-dir",
            str(tmp_path / "cache"),
            "--out",
            str(tmp_path / "out"),
        ]
    )
    assert rc == 2
    err = capsys.readouterr().err
    assert "日線快取不足" in err and "SPY" in err


def test_offline_cli_writes_results_and_report(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    cache = tmp_path / "cache"
    _store(cache)
    out = tmp_path / "out"
    args: list[str] = [
        "advisor-ytd-report",
        "--offline",
        "--force",
        "--universe",
        "AAA,BBB",
        "--ytd-start",
        START,
        "--cache-dir",
        str(cache),
        "--out",
        str(out),
    ]
    assert cli.main(args) == 0
    dirs = list((out / "calibration").glob("advisor-ytd-report_*"))
    assert len(dirs) == 1
    results: dict[str, Any] = json.loads(
        (dirs[0] / "results.json").read_text(encoding="utf-8")
    )
    assert len(results["rows"]) == 11
    assert "機械化解讀與限制" in (dirs[0] / "report.md").read_text(encoding="utf-8")


def test_account_nav_is_rebased_and_absolute_amounts_never_leak(tmp_path: Path) -> None:
    store = _store(tmp_path)
    db = tmp_path / "priv.db"
    conn = sqlite3.connect(db)
    conn.execute(
        "CREATE TABLE portfolio_nav_daily (user_id INTEGER, date TEXT, nav REAL, "
        "positions_json TEXT, created_at TEXT)"
    )
    conn.execute("CREATE TABLE historical_iv (symbol TEXT, iv REAL, date TEXT)")
    for k, d in enumerate(DAYS[-8:]):
        conn.execute(
            "INSERT INTO portfolio_nav_daily VALUES (7, ?, ?, ?, 'x')",
            (d.strftime("%Y-%m-%d"), 123_456.78 * (1.0 + 0.003 * k), '{"ZZZ": 4321}'),
        )
    conn.commit()
    conn.close()
    nav = load_account_nav(db)
    assert nav.iloc[0] == pytest.approx(100_000.0)
    result, md = build_advisor_ytd_report(
        store, ytd_start=START, universe=UNI, snapshot_db=db
    )
    blob = md + json.dumps(result, default=str)
    for needle in ("123456", "123,456", "4321", "ZZZ"):
        assert needle not in blob
    assert result["same_window"]["base"] == 100_000.0
    assert result["same_window"]["rows"][0]["total_return"] == pytest.approx(0.021)
