"""
tests/unit/test_forward_report_quality.py

forward-report 品質修正：
  1. G.2／G.3 與影子否決需 ≥ SQUEEZE_MIN_DATES 個事件交易日才判讀。
  2. 出場分層表排除現金等價標的，並揭露標的數／交易日數／排除筆數。
  3. Gamma Flip 重要性研究渲染進 report.md。
"""

import json
from datetime import date, timedelta
from typing import Any

import pandas as pd

from calibration.config import CalibrationConfig
from calibration.forward_log import (
    EXIT_BREAKDOWN_EXCLUDED_SYMBOLS,
    SQUEEZE_MIN_DATES,
    _exit_n_dates,
    build_exit_tier_breakdown,
    build_forward_report,
    build_squeeze_flip_check,
)
from calibration.report import render_markdown
from market_analysis import evaluation_recorder

CFG = CalibrationConfig(n_boot=100, seed=7)


def _bdays(start: date, n: int) -> list[date]:
    out: list[date] = []
    d = start
    while len(out) < n:
        if d.weekday() < 5:
            out.append(d)
        d += timedelta(days=1)
    return out


DAYS = _bdays(date(2026, 1, 5), 120)


def _row(i: int, symbol: str, decision: int) -> dict[str, Any]:
    return {
        "bar_ts": f"{DAYS[i].isoformat()}T10:00:00-05:00",
        "symbol": symbol,
        "source": "WATCHLIST_ADVISOR",
        "decision": decision,
        "conditions_mask": 1,
    }


def _squeeze_fixture(
    n_dates: int,
) -> tuple[pd.DataFrame, dict[str, list[tuple[date, float]]]]:
    """每個事件日一檔處理標的與一檔對照標的；處理組報酬略高於對照組。"""
    rows: list[dict[str, Any]] = []
    closes: dict[str, list[tuple[date, float]]] = {}
    for i in range(n_dates):
        for sym, dec, slope in ((f"T{i}", 1, 1.0 + 0.01 * (i % 3)), (f"C{i}", 0, 0.5)):
            rows.append(_row(i, sym, dec))
            closes[sym] = [(d, 100.0 + slope * k) for k, d in enumerate(DAYS)]
    return pd.DataFrame(rows), closes


def _results(check: dict[str, Any]) -> dict[str, Any]:
    return {
        "generated_at": "x",
        "config": {},
        "parameters": [],
        "event_tables": [],
        "forward_collection": {
            "status": "OK",
            "total_labeled": 0,
            "sections": [],
            "squeeze_flip_check": check,
            "regime_breakdown": [],
        },
    }


def test_g2_few_dates_not_judged() -> None:
    rows, closes = _squeeze_fixture(2)
    check = build_squeeze_flip_check(rows, closes, CFG)
    g2 = check["primary"]["g2"]
    assert g2["n_dates_treat"] == 2
    assert g2["min_dates"] == SQUEEZE_MIN_DATES
    assert g2["excess"] is not None
    assert g2["t_stat"] is None and g2["g2_passed"] is None
    assert g2["status"] == f"樣本不足（交易日數 2／{SQUEEZE_MIN_DATES}）"
    assert "不判讀 t 值" in check["primary"]["g3"]
    assert "顯著" not in check["primary"]["g3"]
    md = render_markdown(_results(check))
    assert "達標：是" not in md.split("G.2")[1]
    assert "達標：否" not in md.split("G.2")[1]
    assert "達統計顯著" not in md
    assert "樣本不足（交易日數" in md


def test_g2_enough_dates_judged() -> None:
    rows, closes = _squeeze_fixture(SQUEEZE_MIN_DATES)
    check = build_squeeze_flip_check(rows, closes, CFG)
    g2 = check["primary"]["g2"]
    assert g2["n_dates_treat"] == SQUEEZE_MIN_DATES
    assert g2["g2_passed"] is True
    assert g2["t_stat"] is not None
    assert "status" not in g2 or "交易日數" not in str(g2.get("status"))
    assert "交易日數不足" not in str(check["primary"].get("g3"))


def test_render_squeeze_without_new_keys() -> None:
    old = {
        "primary": {
            "g1": {"independent_events": 1, "by_tier": {}, "g1_passed": False},
            "g2": {"n_treat": 1, "n_control": 1, "g2_passed": True, "t_stat": 1.0},
            "g3": "x",
        }
    }
    assert "達標：是" in render_markdown(_results(old))


def _shadow_rows(n_dates: int) -> tuple[pd.DataFrame, dict[str, Any]]:
    recs: list[dict[str, Any]] = []
    cl: dict[str, list[tuple[date, float]]] = {}
    for g, flag in enumerate((True, False)):
        for i in range(12):
            sym = f"S{g}_{i}"
            idx = i % n_dates
            r = _row(idx, sym, 1)
            r["features_json"] = json.dumps({"sv_any": flag})
            recs.append(r)
            cl[sym] = [
                (d, 100.0 if k < idx + 20 else (90.0 if flag else 110.0))
                for k, d in enumerate(DAYS)
            ]
    return pd.DataFrame(recs), cl


def test_shadow_requires_date_floor() -> None:
    rows, closes = _shadow_rows(5)
    e = build_squeeze_flip_check(rows, closes, CFG)["primary"]["shadow_vetoes"][
        "sv_any"
    ]
    assert e["n_flag"] == 12 and e["n_pass"] == 12
    assert e["n_dates_flag"] == 5 and e["n_dates_pass"] == 5
    assert e["verdict"] == "樣本不足"


def _exit_row(symbol: str, day: str) -> dict[str, Any]:
    return {
        "evaluator": "EXIT_SL_REGIME_FLIP",
        "symbol": symbol,
        "bar_ts": f"{day}T10:00:00-04:00",
        "outcome": -1,
        "date": day,
        "features_json": json.dumps({"advisory": False}),
        "fwd_ret_5d": 0.0,
    }


def test_exit_breakdown_excludes_cash_equivalents() -> None:
    assert "BOXX" in EXIT_BREAKDOWN_EXCLUDED_SYMBOLS
    df = pd.DataFrame(
        [
            _exit_row("BOXX", "2026-10-01"),
            _exit_row("boxx", "2026-10-02"),
            _exit_row("SGOV", "2026-10-02"),
            _exit_row("AAPL", "2026-10-01"),
            _exit_row("MSFT", "2026-10-02"),
            _exit_row("MSFT", "2026-10-03"),
        ]
    )
    out = build_exit_tier_breakdown(df)
    assert len(out) == 1
    assert out[0]["n"] == 3
    assert out[0]["n_symbols"] == 2
    assert out[0]["n_dates"] == 3


def test_exit_breakdown_without_symbol_column() -> None:
    df = pd.DataFrame([_exit_row("AAPL", "2026-10-01")]).drop(columns=["symbol"])
    out = build_exit_tier_breakdown(df)
    assert out[0]["n"] == 1 and out[0]["n_symbols"] is None


def test_forward_report_exit_tiers_excluded_count() -> None:
    def lab(symbol: str) -> dict[str, Any]:
        return {
            "evaluator": "EXIT_SL_REGIME_FLIP",
            "decision": 1,
            "direction": "SHORT",
            "first_touch_k15": 1,
            "bar_ts": "2026-10-01T10:00:00-04:00",
            "symbol": symbol,
            "features_json": json.dumps({"advisory": False}),
            "fwd_ret_5d": 0.01,
            "spot": 100.0,
            "next_negative_node": None,
            "atr_1d": 2.0,
            "resistance_wall": None,
            "atr_15m": 0.5,
            "put_wall": None,
            "conditions_mask": None,
            "conditions_evaluated_mask": None,
        }

    rows = pd.DataFrame([lab("BOXX"), lab("BIL"), lab("AAPL")])
    report = build_forward_report(rows, CFG)
    assert report["exit_tiers_excluded"] == 2
    assert sum(e["n"] for e in report["exit_tiers"]) == 1
    md = render_markdown(
        {
            "generated_at": "x",
            "config": {},
            "parameters": [],
            "event_tables": [],
            "forward_collection": report,
        }
    )
    assert "標的數" in md and "交易日數" in md
    assert "已排除現金等價標的" in md and "2 筆" in md


def test_markdown_renders_gamma_flip_materiality() -> None:
    study = {
        "threshold": 0.05,
        "groups": {
            "雜訊交叉": {
                "n": 30,
                "n_dates": 8,
                "win_rate": 0.4,
                "adverse_rate": 0.3,
                "adverse_ci95": [0.2, 0.4],
            },
            "重要交叉": {"n": 5, "n_dates": 3},
        },
        "ratio_quartiles": [
            {
                "bucket": "(0.0, 0.1]",
                "n": 20,
                "n_dates": 5,
                "win_rate": 0.5,
                "win_ci95": [0.3, 0.7],
                "adverse_rate": 0.2,
                "adverse_ci95": [0.1, 0.3],
                "status": "資料累積中 (20/100)",
            }
        ],
        "判讀": "樣本不足 (測試)",
    }
    for value in (study, "資料累積中 (0/100)", None):
        md = render_markdown(
            {
                "generated_at": "x",
                "config": {},
                "parameters": [],
                "event_tables": [],
                "forward_collection": {
                    "status": "OK",
                    "total_labeled": 1,
                    "sections": [],
                    "threshold_studies": {"gamma_flip_materiality": value},
                },
            }
        )
        if isinstance(value, dict):
            assert "Gamma Flip 重要性 (ρ)" in md
            assert "雜訊交叉" in md and "(0.0, 0.1]" in md
            assert "判讀：樣本不足 (測試)" in md
        elif isinstance(value, str):
            assert value in md
            assert "Gamma Flip 重要性" in md
        else:
            assert "Gamma Flip 重要性" not in md


def test_excluded_symbols_in_sync_with_recorder() -> None:
    """報表排除清單須與 evaluation_recorder 的寫入排除清單逐字一致。"""
    assert set(EXIT_BREAKDOWN_EXCLUDED_SYMBOLS) == set(
        evaluation_recorder._EXIT_RECORD_EXCLUDED_SYMBOLS
    )


def test_exit_n_dates_bar_ts_fallback() -> None:
    """無 date 欄時改由 bar_ts 推算事件日數 (同日兩筆只算一天)。"""
    df = pd.DataFrame(
        {
            "bar_ts": [
                "2026-10-01T10:00:00-04:00",
                "2026-10-01T11:00:00-04:00",
                "2026-10-02T10:00:00-04:00",
            ]
        }
    )
    assert _exit_n_dates(df) == 2
    assert _exit_n_dates(pd.DataFrame({"x": [1]})) is None
