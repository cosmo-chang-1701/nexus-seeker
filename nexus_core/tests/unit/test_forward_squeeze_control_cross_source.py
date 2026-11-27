"""forward-report：擠壓 G.2 主結果對照組排除任何來源的進場紀錄 (§5.8 G)。全部合成資料。"""

from datetime import date, timedelta
from typing import Any

import pandas as pd

from calibration.config import CalibrationConfig
from calibration.forward_log import SQUEEZE_T1_WINDOW_SINCE, build_squeeze_flip_check
from calibration.report import render_markdown

CFG = CalibrationConfig(n_boot=200, seed=7)
SINCE = SQUEEZE_T1_WINDOW_SINCE
D = date(2026, 1, 12)


def _bdays(start: date, n: int) -> list[date]:
    out: list[date] = []
    d = start
    while len(out) < n:
        if d.weekday() < 5:
            out.append(d)
        d += timedelta(days=1)
    return out


def _closes(start: date) -> dict[str, list[tuple[date, float]]]:
    series = [(d, 100.0 + i) for i, d in enumerate(_bdays(start, 60))]
    return {s: series for s in ("AAA", "BBB", "CCC", "DDD", "EEE")}


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


def _rows() -> pd.DataFrame:
    return pd.DataFrame(
        [
            _sq(date(2026, 1, 5), symbol="AAA", tier=2),  # 對照期間起點
            _sq(date(2026, 1, 20), symbol="BBB", tier=3),  # 對照期間終點
            _sq(D, symbol="CCC", source="SYMBOL_VIEW"),
            _sq(D, symbol="CCC", decision=0),
            _sq(D + timedelta(days=1), symbol="CCC", decision=0),
            _sq(D, symbol="DDD", decision=0),
            _sq(D, symbol="EEE", decision=0, source="SYMBOL_VIEW"),
        ]
    )


def test_symbol_view_entry_pair_excluded_from_primary_control() -> None:
    res = build_squeeze_flip_check(_rows(), _closes(date(2026, 1, 5)), CFG)
    g2 = res["primary"]["g2"]
    assert g2["n_treat"] == 2
    assert g2["n_control"] == 2
    assert g2["control_excluded_other_source"] == 1


def test_pre_window_symbol_view_t1_still_excluded() -> None:
    excluded_day = date(2026, 11, 9)
    rows = pd.DataFrame(
        [
            _sq(date(2026, 11, 2), symbol="AAA", tier=2),
            _sq(date(2026, 11, 18), symbol="BBB", tier=3),
            _sq(excluded_day, symbol="CCC", tier=1, source="SYMBOL_VIEW"),
            _sq(excluded_day, symbol="CCC", decision=0),
            _sq(excluded_day, symbol="DDD", decision=0),
        ]
    )
    res = build_squeeze_flip_check(
        rows, _closes(date(2026, 11, 2)), CFG, t1_since=SINCE
    )
    assert res["all_sources"]["g1"]["t1_excluded_rows"] == 1
    g2 = res["primary"]["g2"]
    assert g2["n_control"] == 1
    assert g2["control_excluded_other_source"] == 1
    assert res["all_sources"]["g2"]["n_control"] == 1


def test_all_sources_control_unchanged() -> None:
    res = build_squeeze_flip_check(_rows(), _closes(date(2026, 1, 5)), CFG)
    g2 = res["all_sources"]["g2"]
    # (CCC, D+1)、(DDD, D)、(EEE, D)；(CCC, D) 因 SYMBOL_VIEW decision=1 排除
    assert g2["n_control"] == 3
    assert "control_excluded_other_source" not in g2


def test_markdown_shows_cross_source_control_line() -> None:
    check = build_squeeze_flip_check(_rows(), _closes(date(2026, 1, 5)), CFG)
    md = render_markdown(
        {
            "config": {},
            "parameters": [],
            "event_tables": [],
            "data_coverage": {},
            "forward_collection": {
                "status": "OK",
                "total_labeled": 0,
                "sections": [],
                "squeeze_flip_check": check,
                "regime_breakdown": [],
            },
            "generated_at": "x",
            "git_sha": "x",
            "schema_version": 1,
        }
    )
    assert (
        "  - G.2 對照組：已排除其他來源（含 `SYMBOL_VIEW`）有進場紀錄的 (標的, 日) 1 組"
    ) in md
    # 只有主結果輸出此行，全部來源不輸出
    assert md.count("G.2 對照組：") == 1
