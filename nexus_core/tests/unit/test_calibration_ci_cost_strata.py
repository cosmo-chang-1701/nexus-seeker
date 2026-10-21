"""校準報告三項參考統計：深度四分位／分桶 CI、停損墊片成本面、擠壓 G.2 分層。全部合成資料。"""

from datetime import date, timedelta
from typing import Any

import pandas as pd
import pytest

from calibration.config import CalibrationConfig
from calibration.forward_log import _bucket_rows, build_squeeze_flip_check
from calibration.microstructure import (
    _mean_stats,
    depth_quartile_hold_rates,
    stop_buffer_outcomes,
)
from calibration.report import render_markdown

CFG = CalibrationConfig(n_boot=200, seed=7)


# ------------------------------------------------------------------ A1 深度四分位
def _depth_labeled(
    n_dates: int, held_by_q: dict[int, bool], per_q: int = 2
) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    d0 = date(2026, 1, 5)
    for i in range(n_dates):
        d = (d0 + timedelta(days=i)).isoformat()
        for q in range(4):
            for r in range(per_q):
                out.append(
                    {
                        "date": d,
                        "depth_ratio": q * 10 + r,
                        "tested": True,
                        "held": held_by_q[q],
                    }
                )
    return out


def test_depth_quartile_ci_keys_and_insufficient_verdict() -> None:
    res = depth_quartile_hold_rates(_depth_labeled(5, dict.fromkeys(range(4), True)))
    for q in ("Q1", "Q2", "Q3", "Q4"):
        assert set(res[q]) == {"tested", "held", "n_dates", "hold_rate", "ci95"}
        assert res[q]["tested"] == 10
    assert len(res["quartile_cuts"]) == 3
    assert res["判讀"].startswith("樣本不足（可標註日期 5/20")
    assert "Q1" in res["判讀"]


def test_depth_quartile_verdict_q2_clearly_lower() -> None:
    held = {0: True, 1: False, 2: True, 3: True}
    res = depth_quartile_hold_rates(_depth_labeled(24, held))
    assert res["Q2"]["hold_rate"] == 0.0
    assert "明顯低於" in res["判讀"] and "提案" in res["判讀"]


def test_depth_quartile_verdict_no_difference_and_fallback() -> None:
    res = depth_quartile_hold_rates(_depth_labeled(24, dict.fromkeys(range(4), True)))
    assert res["判讀"] == "未見 Q2 明顯低於 Q3／Q4，維持 GEX_WALL_MIN_DEPTH_RATIO"
    assert (
        depth_quartile_hold_rates(_depth_labeled(1, dict.fromkeys(range(4), True), 0))
        == {}
    )


# ------------------------------------------------------------------ A2 分桶 CI
def _bucket_frame(with_date: bool) -> pd.DataFrame:
    data: dict[str, Any] = {
        "q": ["a"] * 6,
        "win": [1, 0, 1, 1, 0, 1],
        "outcome": [1, -1, 1, 1, -1, 1],
    }
    if with_date:
        data["date"] = ["d1", "d1", "d2", "d2", "d3", "d3"]
    return pd.DataFrame(data)


def test_bucket_rows_ci_present_with_dates_none_without() -> None:
    row = _bucket_rows(_bucket_frame(True), "q")[0]
    assert row["n"] == 6 and row["n_dates"] == 3
    for k in ("win_ci95", "adverse_ci95"):
        lo, hi = row[k]
        assert 0.0 <= lo <= hi <= 1.0
    assert row["win_rate"] == pytest.approx(4 / 6)
    row = _bucket_rows(_bucket_frame(False), "q")[0]
    assert row["win_ci95"] is None and row["adverse_ci95"] is None
    assert row["n_dates"] is None


# ------------------------------------------------------------------ B 停損成本
def _ev(d: str, lows: list[float], last: float) -> dict[str, Any]:
    return {
        "date": d,
        "walls": {"edge_PutWall": {"strike": 100.0, "tested": True}},
        "_path": {
            "lows": lows,
            "closes": [100.0, 100.0, 100.0, 100.0, last],
            "atr_1d": 26.0,
        },
    }


def test_mean_stats_empty_and_values() -> None:
    assert _mean_stats([]) == {"n": 0, "n_dates": 0, "mean_pct": None, "ci_pct": None}
    st = _mean_stats([("a", 0.01), ("b", 0.03)])
    assert st["n"] == 2 and st["n_dates"] == 2 and st["mean_pct"] == 2.0


def test_stop_buffer_cost_fields_hand_computed() -> None:
    labeled = [
        _ev("d1", [99.0, 100, 100, 100, 100], 101.0),  # 觸牆、未停損
        _ev("d2", [90.0, 100, 100, 100, 100], 92.0),  # 觸牆、窄墊片停損
        _ev("d3", [101.0, 101, 101, 101, 101], 105.0),  # 未觸牆
    ]
    res = stop_buffer_outcomes(labeled)
    table = res["定義比較"]
    assert "假設進場在牆價" in res["備註"]
    quarter = table["0.25×ATR1D"]  # 墊片 6.5、停損 93.5
    assert quarter["停損距離%"] == {"mean": 6.5, "median": 6.5}
    assert quarter["停損距離ATR1D"] == 0.25
    assert quarter["每事件模擬報酬"]["n"] == 3
    assert quarter["每事件模擬報酬"]["mean_pct"] == pytest.approx(-0.17)
    assert quarter["每事件模擬報酬_觸牆子集"]["n"] == 2
    assert quarter["每事件模擬報酬_觸牆子集"]["mean_pct"] == pytest.approx(-2.75)
    half = table["0.5×ATR1D"]  # 墊片 13、停損 87：d2 不停損，收盤 92
    assert half["每事件模擬報酬"]["mean_pct"] == pytest.approx(
        (1 - 8 + 5) / 3, abs=0.01
    )
    assert half["停損距離%"]["mean"] == 13.0


# ------------------------------------------------------------------ C 擠壓分層
def _bdays(start: date, n: int) -> list[date]:
    out: list[date] = []
    d = start
    while len(out) < n:
        if d.weekday() < 5:
            out.append(d)
        d += timedelta(days=1)
    return out


DAYS = _bdays(date(2026, 1, 5), 120)


def _series(fn: Any) -> list[tuple[date, float]]:
    return [(d, float(fn(i))) for i, d in enumerate(DAYS)]


def _sq(i: int, symbol: str, decision: int, tier: int = 1) -> dict[str, Any]:
    return {
        "bar_ts": f"{DAYS[i].isoformat()}T10:00:00-05:00",
        "symbol": symbol,
        "source": "WATCHLIST_ADVISOR",
        "decision": decision,
        "conditions_mask": tier,
    }


ROWS = pd.DataFrame([_sq(10, "AAA", 1, 1), _sq(30, "AAA", 1, 2), _sq(10, "BBB", 0)])
AAA = _series(lambda i: 100 + i)
BBB = _series(lambda i: 100 + 2 * i)
VOO = _series(lambda i: 200 - 2 * i if i < 25 else 150)


def test_strata_same_day_match_and_market_state() -> None:
    g2 = build_squeeze_flip_check(ROWS, {"AAA": AAA, "BBB": BBB, "VOO": VOO}, CFG)[
        "primary"
    ]["g2"]
    m = g2["strata"]["日期匹配"]
    assert m["n"] == 1 and m["n_dates"] == 1 and m["no_same_day_control"] == 1
    assert m["excess"] == pytest.approx((130 / 110 - 1) - (160 / 120 - 1))
    s = g2["strata"]["市況分層"]
    assert s["下跌"]["n_treat"] == 1 and s["下跌"]["n_control"] == 1
    assert s["下跌"]["excess"] == pytest.approx(m["excess"])
    assert s["盤整"]["n_treat"] == 1 and s["盤整"]["n_control"] == 0
    assert s["盤整"]["status"] == "樣本不足"
    assert s["上漲"]["status"] == "樣本不足"
    assert s["pending_benchmark"] == 0


def test_strata_without_benchmark_and_g2_unchanged() -> None:
    base = build_squeeze_flip_check(ROWS, {"AAA": AAA, "BBB": BBB}, CFG)["primary"]
    assert base["g2"]["strata"]["市況分層"] == "基準價格不可得"
    withb = build_squeeze_flip_check(ROWS, {"AAA": AAA, "BBB": BBB, "VOO": VOO}, CFG)[
        "primary"
    ]
    # 基準價格存在不會變成事件，也不改既有 G.1／G.2／G.3
    assert withb["g1"] == base["g1"]
    assert withb["g1"]["no_price_events"] == 0
    assert withb["g3"] == base["g3"]
    for k in ("n_treat", "n_control", "pending", "excess", "t_stat", "g2_passed"):
        assert withb["g2"][k] == base["g2"][k]
    expected = ((130 / 110 - 1) + (150 / 130 - 1)) / 2 - (160 / 120 - 1)
    assert base["g2"]["excess"] == pytest.approx(expected)
    assert base["g2"]["g2_passed"] is bool(expected >= 0)


# ------------------------------------------------------------------ 報告渲染
def _results(forward: dict[str, Any]) -> dict[str, Any]:
    return {
        "config": {},
        "parameters": [],
        "event_tables": [],
        "data_coverage": {},
        "forward_collection": forward,
        "generated_at": "x",
        "git_sha": "x",
        "schema_version": 1,
    }


def test_render_bucket_ci_and_strata_lines() -> None:
    row = {
        "bucket": "q1",
        "n": 120,
        "n_dates": 8,
        "win_rate": 0.156,
        "adverse_rate": 0.3,
        "win_ci95": [0.1, 0.22],
        "adverse_ci95": None,
        "status": "OK",
    }
    check = build_squeeze_flip_check(ROWS, {"AAA": AAA, "BBB": BBB, "VOO": VOO}, CFG)
    md = render_markdown(
        _results(
            {
                "status": "OK",
                "total_labeled": 1,
                "sections": [],
                "threshold_studies": {"wall_depth_ratio": [row]},
                "squeeze_flip_check": check,
            }
        )
    )
    assert "勝率 [95% CI]" in md
    assert "0.156 [0.100, 0.220]" in md and "0.300 —" in md
    assert "G.2 日期匹配（參考，不影響達標判定）" in md
    assert "G.2 市況分層「下跌」（參考，不影響達標判定）" in md


def test_render_squeeze_without_strata_and_unavailable_benchmark() -> None:
    check = build_squeeze_flip_check(ROWS, {"AAA": AAA, "BBB": BBB}, CFG)
    assert "基準價格不可得" in render_markdown(_results({"squeeze_flip_check": check}))
    del check["primary"]["g2"]["strata"]
    del check["all_sources"]["g2"]["strata"]
    md = render_markdown(_results({"squeeze_flip_check": check}))
    assert "G.2 20 日超額報酬" in md and "日期匹配" not in md
