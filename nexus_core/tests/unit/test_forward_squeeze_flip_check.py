"""forward-report：擠壓乾跑翻轉檢核 (§5.8 G)、體制分組、--snapshot-db 唯讀載入。全部合成資料。"""

import sqlite3
from datetime import date, timedelta
from pathlib import Path
from typing import Any

import pandas as pd

from calibration.config import CalibrationConfig
from calibration.forward_log import (
    _prepare,
    build_regime_breakdown,
    build_squeeze_flip_check,
    load_forward_rows,
    load_squeeze_rows,
)
from calibration.report import render_markdown
from calibration.stats import clustered_bootstrap_diff

CFG = CalibrationConfig(n_boot=200, seed=7)


def _bdays(start: date, n: int) -> list[date]:
    out: list[date] = []
    d = start
    while len(out) < n:
        if d.weekday() < 5:
            out.append(d)
        d += timedelta(days=1)
    return out


def _closes(start: date = date(2026, 1, 5), n: int = 120) -> list[tuple[date, float]]:
    return [(d, 100.0 + i) for i, d in enumerate(_bdays(start, n))]


def _sq(
    d: date,
    symbol: str = "AAA",
    decision: int = 1,
    tier: Any = 1,
    source: str = "WATCHLIST_ADVISOR",
) -> dict[str, Any]:
    return {
        "bar_ts": f"{d.isoformat()}T10:00:00-05:00",
        "symbol": symbol,
        "source": source,
        "decision": decision,
        "conditions_mask": tier,
    }


def test_dedup_same_tier_and_independent_tiers() -> None:
    days = [d for d, _ in _closes()]
    rows = pd.DataFrame(
        [_sq(days[0]), _sq(days[0]), _sq(days[5]), _sq(days[12])]
        + [_sq(days[5], tier=2)]
    )
    res = build_squeeze_flip_check(rows, {"AAA": _closes()}, CFG)["primary"]["g1"]
    assert res["by_tier"][1] == 2  # 第 1、第 3 個（0、12），5 被丟棄
    assert res["by_tier"][2] == 1  # 不同等級不互相去重
    assert res["independent_events"] == 3


def test_g1_pass_requires_every_tier() -> None:
    days = [d for d, _ in _closes(n=400)]
    ev = []
    for i in range(26):
        ev.append(_sq(days[i * 11], tier=1 if i % 2 else 2))
    for j in range(4):
        ev.append(_sq(days[j * 11], symbol="BBB", tier=3))
    closes = {"AAA": _closes(n=400), "BBB": _closes(n=400)}
    g1 = build_squeeze_flip_check(pd.DataFrame(ev), closes, CFG)["primary"]["g1"]
    assert g1["independent_events"] == 30
    assert g1["by_tier"][3] == 4
    assert g1["g1_passed"] is False
    ev.append(_sq(days[4 * 11], symbol="BBB", tier=3))
    g1 = build_squeeze_flip_check(pd.DataFrame(ev), closes, CFG)["primary"]["g1"]
    assert g1["g1_passed"] is True


def test_ret20_pending_and_weekend_event() -> None:
    cl = _closes(n=40)
    days = [d for d, _ in cl]
    saturday = date(2026, 1, 10)
    assert saturday.weekday() == 5
    rows = pd.DataFrame(
        [
            _sq(saturday, tier=1),  # 取下週一為 t
            _sq(days[25], symbol="AAA", tier=2),  # 不滿 20 日
            _sq(days[6], decision=0),
        ]
    )
    g2 = build_squeeze_flip_check(rows, {"AAA": cl}, CFG)["primary"]["g2"]
    assert g2["pending"] == 1
    assert g2["n_treat"] == 1
    # 下週一 (index 5) 收盤 105 -> index 25 收盤 125
    t_pos = days.index(date(2026, 1, 12))
    expected = cl[t_pos + 20][1] / cl[t_pos][1] - 1
    ctrl = cl[26][1] / cl[6][1] - 1
    assert abs(g2["excess"] - (expected - ctrl)) < 1e-12


def test_control_group_dedup_and_period_filter() -> None:
    cl = _closes(n=80)
    days = [d for d, _ in cl]
    rows = pd.DataFrame(
        [
            _sq(days[10], tier=1),
            _sq(days[14], tier=2),
            _sq(days[10], decision=0),
            _sq(days[10], decision=0),  # 同 (symbol, 日) 只取第一筆
            _sq(days[12], decision=0),
            _sq(days[2], decision=0),  # 期間外
            _sq(days[40], decision=0),  # 期間外
        ]
    )
    g2 = build_squeeze_flip_check(rows, {"AAA": cl}, CFG)["primary"]["g2"]
    assert g2["n_control"] == 2


def test_clustered_bootstrap_diff_reproducible_and_zero() -> None:
    v = [0.1, -0.2, 0.3, 0.05, 0.0, 0.12]
    c = ["d1", "d2", "d3", "d4", "d5", "d6"]
    a = clustered_bootstrap_diff(v, c, v, c, 300, 7)
    b = clustered_bootstrap_diff(v, c, v, c, 300, 7)
    assert a == b
    diff, lo, hi, _se = a
    assert diff == 0.0
    assert lo <= 0.0 <= hi


def test_empty_closes_price_unavailable_no_exception() -> None:
    rows = pd.DataFrame([_sq(date(2026, 2, 2)), _sq(date(2026, 2, 3), decision=0)])
    res = build_squeeze_flip_check(rows, {}, CFG)
    assert res["primary"]["g2"]["status"] == "價格不可得"
    assert res["primary"]["g1"]["independent_events"] == 1
    assert build_squeeze_flip_check(pd.DataFrame(), {}, CFG)["n_rows"] == 0


def test_source_stratification() -> None:
    cl = _closes(n=60)
    days = [d for d, _ in cl]
    rows = pd.DataFrame(
        [_sq(days[0], source="SYMBOL_VIEW", tier=1)]
        + [_sq(days[1], source="WATCHLIST_ADVISOR", tier=2)]
    )
    res = build_squeeze_flip_check(rows, {"AAA": cl}, CFG)
    assert res["primary"]["g1"]["by_tier"][1] == 0
    assert res["primary"]["g1"]["by_tier"][2] == 1
    assert res["all_sources"]["g1"]["by_tier"][1] == 1
    assert res["primary"]["g1"]["g1_passed"] is False


def _fwd_row(i: int, regime: str, source: str, touch: Any) -> dict[str, Any]:
    return {
        "evaluator": "REGIME_CLASSIFIER",
        "regime": regime,
        "source": source,
        "decision": 1,
        "direction": "LONG",
        "bar_ts": f"2026-03-{(i % 28) + 1:02d}T10:00:00-05:00",
        "first_touch_k15": touch,
    }


def test_regime_breakdown_groups_ci_and_timeout() -> None:
    rows = [
        _fwd_row(
            i, "III", "WATCHLIST_ADVISOR", 1 if i % 3 == 0 else -1 if i % 3 == 1 else 0
        )
        for i in range(40)
    ]
    rows += [_fwd_row(i, "I", "SYMBOL_VIEW", 1) for i in range(5)]
    out = build_regime_breakdown(_prepare(pd.DataFrame(rows), "first_touch_k15"), CFG)
    by = {(r["regime"], r["source"]): r for r in out}
    big, small = by[("III", "WATCHLIST_ADVISOR")], by[("I", "SYMBOL_VIEW")]
    assert big["n"] == 40 and big["expectancy_ci"] is not None
    assert (
        abs(big["timeout_rate"] - (1 - big["win_rate"] - big["adverse_rate"])) < 1e-12
    )
    assert small["expectancy_ci"] is None
    assert small["status"] == "資料累積中 (5/100)"
    assert small["n_dates"] == 5


def _make_db(path: Path) -> None:
    conn = sqlite3.connect(path)
    conn.executescript(
        """
        CREATE TABLE regime_evaluation_log (
            id INTEGER PRIMARY KEY, bar_ts TEXT, symbol TEXT, source TEXT,
            evaluator TEXT, sub_mode TEXT, decision INTEGER, conditions_mask INTEGER,
            direction TEXT, regime TEXT);
        CREATE TABLE regime_evaluation_outcome (
            evaluation_id INTEGER, label_status TEXT, first_touch_k1 INTEGER,
            first_touch_k15 INTEGER, first_touch_k2 INTEGER, fwd_ret_1d REAL,
            fwd_ret_5d REAL, plan_outcome TEXT, max_up_atr_5d REAL, max_down_atr_5d REAL);
        INSERT INTO regime_evaluation_log VALUES
          (1,'2026-03-02T10:00:00-05:00','AAA','WATCHLIST_ADVISOR','ENTRY_SQUEEZE','ENTRY',1,2,'LONG',NULL),
          (2,'2026-03-03T10:00:00-05:00','AAA','WATCHLIST_ADVISOR','ENTRY_SQUEEZE','WATCH',0,NULL,'LONG',NULL),
          (3,'2026-03-03T10:00:00-05:00','BBB','SYMBOL_VIEW','REGIME_CLASSIFIER',NULL,1,NULL,'LONG','III');
        INSERT INTO regime_evaluation_outcome VALUES (3,'LABELED',1,1,1,0.01,0.02,NULL,1.0,0.5);
        """
    )
    conn.commit()
    conn.close()


def test_load_squeeze_rows_includes_unlabeled(tmp_path: Path) -> None:
    db = tmp_path / "snap.db"
    _make_db(db)
    rows = load_squeeze_rows(db)
    assert len(rows) == 2  # 兩列皆未標註
    assert set(rows["symbol"]) == {"AAA"}


def test_load_forward_rows_snapshot_readonly(tmp_path: Path) -> None:
    db = tmp_path / "snap.db"
    _make_db(db)
    rows = load_forward_rows(db)
    assert list(rows["symbol"]) == ["BBB"]
    ro = sqlite3.connect(f"{db.resolve().as_uri()}?mode=ro", uri=True)
    ro.close()


def test_report_renders_squeeze_section() -> None:
    cl = _closes(n=60)
    rows = pd.DataFrame([_sq(cl[0][0])])
    forward = {
        "status": "OK",
        "total_labeled": 0,
        "sections": [],
        "squeeze_flip_check": build_squeeze_flip_check(rows, {"AAA": cl}, CFG),
        "regime_breakdown": [],
    }
    results = {
        "config": {},
        "parameters": [],
        "event_tables": [],
        "data_coverage": {},
        "forward_collection": forward,
        "generated_at": "x",
        "git_sha": "x",
        "schema_version": 1,
    }
    md = render_markdown(results)
    assert "擠壓乾跑翻轉檢核" in md
    assert "SQUEEZE_ENTRY_DRY_RUN" in md
