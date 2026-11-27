"""forward-report：擠壓翻轉檢核的 T1 窗口樣本切分 (§5.8 G)。全部合成資料。"""

import asyncio
from datetime import date, timedelta
from typing import Any

import pandas as pd
import pytest

from calibration import __main__ as cli
from calibration import forward_log
from calibration.config import CalibrationConfig
from calibration.forward_log import SQUEEZE_T1_WINDOW_SINCE, build_squeeze_flip_check
from calibration.report import render_markdown

CFG = CalibrationConfig(n_boot=200, seed=7)
SINCE = SQUEEZE_T1_WINDOW_SINCE


def _bdays(start: date, n: int) -> list[date]:
    out: list[date] = []
    d = start
    while len(out) < n:
        if d.weekday() < 5:
            out.append(d)
        d += timedelta(days=1)
    return out


def _closes(start: date = date(2026, 11, 2), n: int = 60) -> list[tuple[date, float]]:
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


def _mixed_rows() -> pd.DataFrame:
    return pd.DataFrame(
        [
            _sq(date(2026, 11, 5), symbol="AAA", tier=1),  # 切分前 T1：排除
            _sq(date(2026, 11, 13), symbol="BBB", tier=1),  # 切分前一日 T1：排除
            _sq(date(2026, 11, 6), symbol="CCC", tier=1, source="SYMBOL_VIEW"),
            _sq(date(2026, 11, 5), symbol="DDD", tier=2),  # 前期 T2 照計
            _sq(date(2026, 11, 5), symbol="EEE", tier=3),  # 前期 T3 照計
            _sq(SINCE, symbol="FFF", tier=1),  # 切分日當天照計
            _sq(date(2026, 11, 17), symbol="GGG", tier=1),
        ]
    )


def _mixed_closes() -> dict[str, list[tuple[date, float]]]:
    return {s: _closes() for s in ("AAA", "BBB", "CCC", "DDD", "EEE", "FFF", "GGG")}


def test_pre_window_t1_excluded_from_g1_g2() -> None:
    res = build_squeeze_flip_check(_mixed_rows(), _mixed_closes(), CFG, t1_since=SINCE)
    assert res["t1_window_since"] == "2026-11-14"

    g1 = res["primary"]["g1"]
    assert g1["by_tier"] == {1: 2, 2: 1, 3: 1}
    assert g1["independent_events"] == 4
    assert g1["decision1_rows"] == 6  # 原始列數語意不變
    assert g1["t1_since"] == "2026-11-14"
    assert g1["t1_excluded_rows"] == 2
    assert g1["unsegmented"]["independent_events"] == 6
    assert g1["unsegmented"]["by_tier"] == {1: 4, 2: 1, 3: 1}
    assert g1["unsegmented"]["g1_passed"] is False

    assert res["primary"]["g2"]["n_treat"] == 4
    # 影子否決與 G.2 同母體
    assert res["primary"]["shadow_vetoes"]["sv_any"]["n_null"] == 4

    # 不看觸發來源：SYMBOL_VIEW 的前期 T1 在全部來源亦排除
    all_g1 = res["all_sources"]["g1"]
    assert all_g1["t1_excluded_rows"] == 3
    assert all_g1["by_tier"] == {1: 2, 2: 1, 3: 1}
    assert res["all_sources"]["g2"]["n_treat"] == 4


def test_pre_window_t1_does_not_block_dedup() -> None:
    # 30 根日線：11/12 已滿 20 日、11/17 未滿，可由此辨認被保留的事件
    cl = _closes(n=30)
    rows = pd.DataFrame([_sq(date(2026, 11, 12)), _sq(date(2026, 11, 17))])

    base = build_squeeze_flip_check(rows, {"AAA": cl}, CFG)["primary"]
    assert base["g1"]["independent_events"] == 1
    assert (base["g2"]["n_treat"], base["g2"]["pending"]) == (1, 0)

    res = build_squeeze_flip_check(rows, {"AAA": cl}, CFG, t1_since=SINCE)["primary"]
    assert res["g1"]["by_tier"][1] == 1
    assert (res["g2"]["n_treat"], res["g2"]["pending"]) == (0, 1)


def test_excluded_pairs_not_in_control() -> None:
    excluded_day = date(2026, 11, 9)
    others = [
        _sq(date(2026, 11, 3), tier=2),  # 對照期間起點
        _sq(date(2026, 11, 18), symbol="BBB", tier=3),  # 對照期間終點
        _sq(excluded_day, decision=0),  # 與被排除列同 (標的, 日)
        _sq(date(2026, 11, 10), decision=0),
        _sq(excluded_day, symbol="BBB", decision=0),
    ]
    closes = {"AAA": _closes(), "BBB": _closes()}
    rows = pd.DataFrame([_sq(excluded_day, tier=1), *others])
    res = build_squeeze_flip_check(rows, closes, CFG, t1_since=SINCE)["primary"]
    assert res["g1"]["t1_excluded_rows"] == 1
    assert res["g2"]["n_treat"] == 2
    assert res["g2"]["n_control"] == 2

    # 對照：沒有該 decision=1 列時，同日的 decision=0 會進入對照組
    without = build_squeeze_flip_check(
        pd.DataFrame(others), closes, CFG, t1_since=SINCE
    )["primary"]
    assert without["g2"]["n_control"] == 3


def test_default_t1_since_unchanged() -> None:
    rows, closes = _mixed_rows(), _mixed_closes()
    default = build_squeeze_flip_check(rows, closes, CFG)
    explicit = build_squeeze_flip_check(rows, closes, CFG, t1_since=None)
    assert default == explicit
    assert default["t1_window_since"] is None
    for key in ("primary", "all_sources"):
        g1 = default[key]["g1"]
        assert set(g1) == {
            "independent_events",
            "by_tier",
            "decision1_rows",
            "no_price_events",
            "g1_passed",
        }
    assert default["primary"]["g1"]["by_tier"] == {1: 4, 2: 1, 3: 1}
    empty = build_squeeze_flip_check(pd.DataFrame(), {}, CFG, t1_since=SINCE)
    assert empty["primary"]["g1"]["t1_excluded_rows"] == 0


def _markdown(check: dict[str, Any]) -> str:
    return render_markdown(
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


def test_markdown_shows_t1_window_line() -> None:
    rows, closes = _mixed_rows(), _mixed_closes()
    md = _markdown(build_squeeze_flip_check(rows, closes, CFG, t1_since=SINCE))
    assert (
        "  - T1 窗口切分（§5.8 G）：等級1 僅計 2026-11-14 起；"
        "排除起日前的等級1 決策列 2 筆；未切分（參考，不參與判定）："
        "獨立事件 6（等級1＝4、等級2＝1、等級3＝1）、達標：否"
    ) in md
    assert "T1 窗口切分" not in _markdown(build_squeeze_flip_check(rows, closes, CFG))


def test_cli_passes_t1_window_constant(monkeypatch: pytest.MonkeyPatch) -> None:
    rows = pd.DataFrame([_sq(date(2026, 11, 17))])
    captured: dict[str, Any] = {}

    def _fake_load(snapshot: Any = None) -> pd.DataFrame:
        return rows

    def _fake_build(*args: Any, **kwargs: Any) -> dict[str, Any]:
        captured.update(kwargs)
        return {"ok": True}

    monkeypatch.setattr(forward_log, "load_squeeze_rows", _fake_load)
    monkeypatch.setattr(forward_log, "build_squeeze_flip_check", _fake_build)
    result = asyncio.run(cli._squeeze_flip_check(CalibrationConfig(offline=True), None))
    assert result == {"ok": True}
    assert captured["t1_since"] == date(2026, 11, 14)
