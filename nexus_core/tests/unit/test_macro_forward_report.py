"""總經乾跑前向報告：狀態區段、指標亮起時段、之後 20／60 個交易日報酬。"""

from datetime import date, timedelta
from pathlib import Path
from typing import Any

import pytest

from calibration.macro_forward_report import (
    build_report,
    equal_weight_return,
    forward_return,
    lit_periods,
    render_markdown,
    state_segments,
    write_report,
)


def _prices(start: date, values: list[float]) -> list[tuple[date, float]]:
    return [(start + timedelta(days=i), v) for i, v in enumerate(values)]


def test_state_segments_skip_warmup_and_merge_runs() -> None:
    regimes: list[dict[str, Any]] = [
        {"trading_date": "2026-09-01", "confirmed_state": None},
        {"trading_date": "2026-09-02", "confirmed_state": "GOOD"},
        {"trading_date": "2026-09-03", "confirmed_state": "GOOD"},
        {"trading_date": "2026-09-04", "confirmed_state": "WORST"},
    ]
    segs = state_segments(regimes)
    assert [(s.state, s.start, s.end, s.days) for s in segs] == [
        ("GOOD", "2026-09-02", "2026-09-03", 2),
        ("WORST", "2026-09-04", "2026-09-04", 1),
    ]


def test_lit_periods_contiguous_true_flags() -> None:
    signals: list[dict[str, Any]] = [
        {"trading_date": "2026-09-01", "indicator": "fin_stress", "flag": False},
        {"trading_date": "2026-09-02", "indicator": "fin_stress", "flag": True},
        {"trading_date": "2026-09-03", "indicator": "fin_stress", "flag": True},
        {"trading_date": "2026-09-04", "indicator": "fin_stress", "flag": None},
        {"trading_date": "2026-09-05", "indicator": "fin_stress", "flag": True},
    ]
    assert lit_periods(signals)["fin_stress"] == [
        ("2026-09-02", "2026-09-03"),
        ("2026-09-05", "2026-09-05"),
    ]


def test_forward_return_needs_enough_future_data() -> None:
    p = _prices(date(2026, 1, 1), [100.0 + i for i in range(30)])
    assert forward_return(p, date(2026, 1, 1), 20) == pytest.approx(0.2)
    assert forward_return(p, date(2026, 1, 20), 20) is None
    pool = {"A": p, "B": _prices(date(2026, 1, 1), [100.0] * 30)}
    assert equal_weight_return(pool, date(2026, 1, 1), 20) == pytest.approx(0.1)


def test_build_and_write_report(tmp_path: Path) -> None:
    regimes = [
        {"trading_date": "2026-01-01", "confirmed_state": "CAUTION"},
        {"trading_date": "2026-01-02", "confirmed_state": "CAUTION"},
    ]
    tech = {"A": _prices(date(2026, 1, 1), [100.0 - i * 0.5 for i in range(80)])}
    voo = _prices(date(2026, 1, 1), [100.0] * 80)
    boxx = _prices(date(2026, 1, 1), [100.0 + i * 0.01 for i in range(80)])
    result = build_report(regimes, [], tech, voo, boxx)
    seg = result["segments"][0]
    assert seg["state"] == "CAUTION" and seg["days"] == 2
    assert seg["tech_minus_voo_20d"] < 0  # 轉差後科技池相對轉弱 → 訊號領先
    assert result["state_days"] == {"CAUTION": 2}
    md = render_markdown(result)
    assert "CAUTION" in md and "科技−VOO 20d" in md
    target = write_report(tmp_path, result)
    assert target.exists() and (target.parent / "report.md").exists()
