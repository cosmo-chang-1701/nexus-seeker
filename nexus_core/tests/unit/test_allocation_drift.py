"""allocation_drift 純邏輯單元測試。"""

from __future__ import annotations

import ast
import sys
from datetime import date
from pathlib import Path
from typing import Optional

import pytest

from market_analysis import allocation_drift as ad
from market_analysis.allocation_drift import (
    AllocationBreakdown,
    DriftState,
    compute_breakdown,
    is_outside,
    next_state,
    parse_state,
    reminder_due,
    reserve_amount,
    state_to_json,
)


def _bd(
    shares: dict[str, float],
    closes: dict[str, float],
    cash: float = 0.0,
    target: float = 100.0,
    reserve: float = 0.0,
) -> AllocationBreakdown:
    b = compute_breakdown(shares, closes, cash, target, reserve)
    assert b is not None
    return b


def test_all_boxx_target_100() -> None:
    b = _bd({"BOXX": 10}, {"BOXX": 100.0})
    assert b.equity_pct == 0
    assert b.drift_pp == -100
    assert b.move_amount == b.investable == 1000.0
    assert is_outside(b)


def test_threshold_boundary() -> None:
    small = _bd({"AAPL": 97, "BOXX": 3}, {"AAPL": 1.0, "BOXX": 1.0})
    assert small.drift_pp == pytest.approx(-3)
    assert not is_outside(small)
    edge = _bd({"AAPL": 95, "BOXX": 5}, {"AAPL": 1.0, "BOXX": 1.0})
    assert edge.drift_pp == pytest.approx(-5)
    assert is_outside(edge)


def test_target_40_with_60_stock() -> None:
    b = _bd({"AAPL": 60, "BOXX": 40}, {"AAPL": 1.0, "BOXX": 1.0}, target=40)
    assert b.drift_pp == pytest.approx(20)
    assert b.move_amount == pytest.approx(-20)


def test_shorts_and_boxx_not_equity() -> None:
    b = _bd(
        {"AAPL": 10, "TSLA": -5, "BOXX": 10},
        {"AAPL": 10.0, "TSLA": 100.0, "BOXX": 100.0},
    )
    assert b.equity_value == 100.0
    assert b.boxx_value == 1000.0
    assert b.missing_prices == ()


def test_missing_prices_listed_and_excluded() -> None:
    b = _bd(
        {"AAPL": 10, "MSFT": 5, "NVDA": 3, "BOXX": 1},
        {"AAPL": 10.0, "NVDA": 0.0},
        cash=50.0,
    )
    assert b.missing_prices == ("BOXX", "MSFT", "NVDA")
    assert b.equity_value == 100.0
    assert b.boxx_value == 0.0
    # 缺價的空頭不列入
    c = _bd({"AAPL": 1, "X": -2}, {"AAPL": 1.0})
    assert c.missing_prices == ()


def test_reserve_clamped_and_reduces_investable() -> None:
    b = _bd({"AAPL": 50, "BOXX": 50}, {"AAPL": 1.0, "BOXX": 1.0}, cash=10, reserve=20)
    assert b.reserve == 20
    assert b.investable == 90
    big = _bd(
        {"AAPL": 50, "BOXX": 50}, {"AAPL": 1.0, "BOXX": 1.0}, cash=10, reserve=999
    )
    assert big.reserve == 60
    assert big.investable == 50
    neg = _bd({"AAPL": 50, "BOXX": 50}, {"AAPL": 1.0, "BOXX": 1.0}, reserve=-5)
    assert neg.reserve == 0


def test_reserve_covers_boxx_zero_drift() -> None:
    b = _bd({"AAPL": 1000, "BOXX": 100}, {"AAPL": 1.0, "BOXX": 1.0}, reserve=100)
    assert b.equity_pct == pytest.approx(100)
    assert b.drift_pp == pytest.approx(0)
    assert not is_outside(b)


def test_investable_nonpositive_and_target_clamp() -> None:
    assert compute_breakdown({}, {}, 0.0, 50.0) is None
    assert compute_breakdown({"BOXX": 1}, {"BOXX": 100.0}, 0.0, 50.0, 100.0) is None
    assert compute_breakdown({"AAPL": 1}, {"AAPL": 1.0}, -100.0, 50.0) is not None
    hi = _bd({"AAPL": 1}, {"AAPL": 1.0}, target=150)
    assert hi.target_pct == 100
    lo = _bd({"AAPL": 1}, {"AAPL": 1.0}, target=-10)
    assert lo.target_pct == 0


def test_reserve_amount_windows() -> None:
    jan = date(2027, 1, 4)
    assert reserve_amount(date(2026, 12, 1), jan, 500.0) == 500.0
    assert reserve_amount(date(2026, 11, 30), jan, 500.0) == 0.0
    assert reserve_amount(date(2027, 1, 3), jan, 500.0) == 500.0
    assert reserve_amount(jan, jan, 500.0) == 0.0
    jul = date(2027, 7, 5)
    assert reserve_amount(date(2027, 6, 1), jul, 300.0) == 300.0
    assert reserve_amount(date(2027, 5, 31), jul, 300.0) == 0.0
    assert reserve_amount(date(2027, 6, 1), None, 300.0) == 0.0
    assert reserve_amount(date(2027, 6, 1), jul, 0.0) == 0.0


def test_reminder_sequence() -> None:
    d1 = date(2026, 11, 11)
    state: Optional[DriftState] = None
    assert reminder_due(d1, True, state, 100.0) == "CROSSING"
    state = next_state(d1, True, state, True, 100.0)
    assert state == DriftState(True, "2026-11-11", 100.0)
    d2 = date(2026, 11, 12)
    assert reminder_due(d2, True, state, 100.0) is None
    assert next_state(d2, True, state, False, 100.0) == state
    d3 = date(2026, 12, 1)
    assert reminder_due(d3, True, state, 100.0) == "MONTHLY"
    state = next_state(d3, True, state, True, 100.0)
    # 回到門檻內
    d4 = date(2026, 12, 2)
    assert reminder_due(d4, False, state, 100.0) is None
    state = next_state(d4, False, state, False, 100.0)
    assert state == DriftState(False, "2026-12-01", 100.0)
    # 同月再次超標
    d5 = date(2026, 12, 3)
    assert reminder_due(d5, True, state, 100.0) == "CROSSING"
    # 未送達：狀態不變，隔天仍到期
    assert next_state(d5, True, state, False, 100.0) == state
    assert reminder_due(date(2026, 12, 4), True, state, 100.0) == "CROSSING"
    # 無狀態且未送達 → None
    assert next_state(d1, True, None, False, 100.0) is None
    # 從未有狀態就在界內
    assert next_state(d1, False, None, False, 60.0) == DriftState(False, None, 60.0)


def test_parse_state_roundtrip_and_bad_inputs() -> None:
    s = DriftState(True, "2026-11-11", 60.0)
    assert parse_state(state_to_json(s)) == s
    s2 = DriftState(False, None, None)
    assert parse_state(state_to_json(s2)) == s2
    assert parse_state(None) is None
    assert parse_state("x") is None
    assert parse_state({"outside": "y"}) is None
    assert parse_state({"outside": True, "last_sent": "bad"}) is None
    old = parse_state({"outside": True, "last_sent": "2026-11-11"})
    assert old == DriftState(True, "2026-11-11", None)


def test_target_change_is_new_crossing() -> None:
    state = DriftState(True, "2026-11-11", 100.0)
    assert reminder_due(date(2026, 11, 20), True, state, 60.0) == "CROSSING"
    old = DriftState(True, "2026-11-11", None)
    assert reminder_due(date(2026, 11, 20), True, old, 60.0) == "CROSSING"


def test_module_is_pure_stdlib() -> None:
    src = Path(ad.__file__).read_text(encoding="utf-8")
    names: set[str] = set()
    for node in ast.walk(ast.parse(src)):
        if isinstance(node, ast.Import):
            names.update(a.name.split(".")[0] for a in node.names)
        elif isinstance(node, ast.ImportFrom):
            assert node.level == 0, "不得使用相對匯入"
            if node.module:
                names.add(node.module.split(".")[0])
    assert names
    for n in names:
        assert n == "__future__" or n in sys.stdlib_module_names, n


def test_parse_state_rejects_basic_iso_date() -> None:
    assert ad.parse_state({"outside": True, "last_sent": "20261111"}) is None
    ok = ad.parse_state({"outside": True, "last_sent": "2026-11-11"})
    assert ok is not None and ok.last_sent == "2026-11-11"


@pytest.mark.parametrize("cash,reserve", [(float("nan"), 0.0), (0.0, float("nan"))])
def test_compute_breakdown_non_finite_cash_or_reserve_is_none(
    cash: float, reserve: float
) -> None:
    assert (
        ad.compute_breakdown({"AAPL": 10}, {"AAPL": 10.0}, cash, 50.0, reserve) is None
    )
