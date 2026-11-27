"""forward-report：Skew 門檻判讀所需 CANONICAL 紀錄交易日數 (§5.13)。全部合成資料。"""

import json
from datetime import date, timedelta
from typing import Any, Optional

import pandas as pd

from calibration.config import CalibrationConfig
from calibration.forward_log import (
    SKEW_MIN_CANONICAL_DATES,
    build_forward_report,
    build_threshold_studies,
)
from calibration.report import render_markdown

CFG = CalibrationConfig(n_boot=100, seed=7)
NOT_REACHED = "未達，不判讀 Skew 門檻"
REACHED = "已達，可依 §5.13 判讀（各分位區間仍須 n ≥ 100）"


def _row(
    day: str,
    symbol: str = "AAA",
    skew_source: str = "CANONICAL",
    pct: Optional[float] = 50.0,
    evaluator: str = "REGIME_CLASSIFIER",
    hour: int = 10,
) -> dict[str, Any]:
    return {
        "evaluator": evaluator,
        "regime": "I",
        "source": "WATCHLIST_ADVISOR",
        "symbol": symbol,
        "bar_ts": f"{day}T{hour:02d}:00:00-05:00",
        "date": day,
        "decision": 1,
        "direction": "LONG",
        "first_touch_k15": 1,
        "win": 1,
        "outcome": 1,
        "fwd_ret_5d": 0.01,
        "spot": 100.0,
        "next_negative_node": 90.0,
        "atr_1d": 2.0,
        "resistance_wall": 110.0,
        "atr_15m": 0.5,
        "put_wall": 95.0,
        "conditions_mask": None,
        "conditions_evaluated_mask": None,
        "features_json": json.dumps(
            {"skew_percentile": pct, "skew_percentile_source": skew_source}
        ),
    }


def test_canonical_dates_count_distinct_event_days() -> None:
    entries = pd.DataFrame(
        [
            _row("2026-11-02"),
            _row("2026-11-02", symbol="BBB"),
            _row("2026-11-02", hour=11),
            _row("2026-11-03", symbol="BBB", evaluator="ENTRY_SQUEEZE"),
            _row("2026-11-04", symbol="CCC", pct=99.0),
            _row("2026-11-05", skew_source="INTRADAY_FALLBACK"),
            _row("2026-11-06", skew_source="INTRADAY_FALLBACK"),
            _row("2026-11-09", pct=None),
        ]
    )
    res = build_threshold_studies(entries)["skew_canonical_dates"]
    assert res == {
        "n_dates": 3,
        "min_dates": SKEW_MIN_CANONICAL_DATES,
        "status": NOT_REACHED,
    }

    # 沒有 skew_percentile 欄位時為 0 日、未達
    bare = entries.assign(features_json="{}")
    none = build_threshold_studies(bare)["skew_canonical_dates"]
    assert (none["n_dates"], none["status"]) == (0, NOT_REACHED)

    # 空字串日期不計
    blank = build_threshold_studies(
        pd.DataFrame([_row("2026-11-02"), {**_row("2026-11-03"), "date": ""}])
    )["skew_canonical_dates"]
    assert blank["n_dates"] == 1


def test_canonical_dates_status_reaches_min() -> None:
    days = [(date(2026, 8, 1) + timedelta(days=i)).isoformat() for i in range(60)]
    short = build_threshold_studies(pd.DataFrame([_row(d) for d in days[:59]]))
    assert short["skew_canonical_dates"]["n_dates"] == 59
    assert short["skew_canonical_dates"]["status"] == NOT_REACHED

    res = build_threshold_studies(pd.DataFrame([_row(d) for d in days]))
    canonical = res["skew_canonical_dates"]
    assert canonical["n_dates"] == 60
    assert canonical["min_dates"] == 60
    assert canonical["status"] == REACHED


def _report_rows() -> pd.DataFrame:
    return pd.DataFrame(
        [
            _row("2026-11-02"),
            _row("2026-11-03", evaluator="ENTRY_SQUEEZE"),
            _row("2026-11-04", evaluator="EXIT_STOP"),
            _row("2026-11-05", evaluator="EXIT_TAKE_PROFIT"),
        ]
    ).drop(columns=["date", "win", "outcome"])


def test_canonical_dates_exit_rows_excluded() -> None:
    report = build_forward_report(_report_rows(), CFG)
    assert report["threshold_studies"]["skew_canonical_dates"]["n_dates"] == 2


def test_markdown_shows_canonical_dates_line() -> None:
    md = render_markdown(
        {
            "config": {},
            "parameters": [],
            "event_tables": [],
            "data_coverage": {},
            "forward_collection": build_forward_report(_report_rows(), CFG),
            "generated_at": "x",
            "git_sha": "x",
            "schema_version": 1,
        }
    )
    assert (
        "- Skew 門檻 `CANONICAL` 紀錄交易日數：2／60（以實際紀錄日計，§5.13）；"
        f"{NOT_REACHED}"
    ) in md
