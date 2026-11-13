"""regime_hedge_sizing 純計算單元測試。"""

from datetime import date

import pytest

from market_analysis.dynamic_rollover import regime_hedge_sizing as rhs
from market_analysis.dynamic_rollover.regime_hedge_sizing import (
    HedgePlan,
    HedgePosition,
    build_tier1_plan,
    build_tier2_plan,
    classify_hedge_etf,
    earnings_isolated_symbols,
    equivalent_net_delta_pct,
    leverage_multiplier,
    split_long_exposure,
)

TODAY = date(2026, 11, 12)
PRICES = {"PSQ": 30.0, "SQQQ": 10.0, "SOXS": 20.0}


def _pos(**kw: float) -> list[HedgePosition]:
    return [HedgePosition(k, v) for k, v in kw.items()]


def _check_invariant(
    plan: HedgePlan, positions: list[HedgePosition], nav: float
) -> None:
    longs = sum(
        p.market_value
        for p in positions
        if rhs.leverage_multiplier(p.symbol) == 1.0
        and p.symbol not in plan.liquidate_symbols
    )
    inverse = sum(
        p.market_value for p in positions if p.symbol in rhs.INVERSE_ETF_LEVERAGE
    )
    longs_after = longs - plan.sell_for_funding_value
    assert longs_after >= -1e-6
    new_inv = sum(leg.target_value for leg in plan.legs)
    assert longs_after + inverse + new_inv + plan.boxx_target_value <= nav + 1e-6
    assert plan.boxx_target_value >= 0


def test_leverage_multiplier_boxx_zero_psq_minus1_sqqq_minus3() -> None:
    assert leverage_multiplier("BOXX") == 0.0
    assert leverage_multiplier("PSQ") == -1.0
    assert leverage_multiplier("SQQQ") == -3.0
    assert leverage_multiplier("SOXS") == -3.0
    assert leverage_multiplier("NVDA") == 1.0


def test_equivalent_net_delta_and_nav_guard() -> None:
    pos = _pos(NVDA=50_000, PSQ=10_000, SQQQ=10_000, BOXX=30_000)
    assert equivalent_net_delta_pct(pos, 100_000) == pytest.approx(10.0)
    assert equivalent_net_delta_pct(pos, 0) == 0.0
    assert rhs.is_within_net_delta_guard(-120.0)
    assert not rhs.is_within_net_delta_guard(-121.0)
    assert not rhs.is_within_net_delta_guard(146.0)


def test_classify_and_split() -> None:
    assert classify_hedge_etf("NVDA") == "SOXS"
    assert classify_hedge_etf("MU") == "SOXS"
    assert classify_hedge_etf("MSFT") == "SQQQ"
    assert classify_hedge_etf("XOM") == "SQQQ"
    split = split_long_exposure(_pos(NVDA=10, MSFT=20, BOXX=5, PSQ=7))
    assert split == {"SOXS": 10, "SQQQ": 20}


def test_earnings_isolated_symbols_window() -> None:
    d = {
        "A": date(2026, 11, 12),
        "B": date(2026, 11, 14),
        "C": date(2026, 11, 15),
        "D": None,
        "E": date(2026, 11, 11),
    }
    assert earnings_isolated_symbols(["E", "D", "C", "B", "A", "Z"], d, TODAY) == (
        "A",
        "B",
    )


def test_tier1_never_emits_minus3x_legs() -> None:
    pos = _pos(NVDA=40_000, MSFT=40_000, BOXX=20_000)
    plan = build_tier1_plan(pos, 100_000, ["NVDA"], 20_000, PRICES)
    assert plan.tier == 1 and plan.mode == "TIER1_PSQ"
    assert all(leg.symbol == "PSQ" for leg in plan.legs)
    assert plan.legs[0].shares == 1000  # 40% 跌破 → 上限 30k / 30


@pytest.mark.parametrize(
    "broken_value,expected",
    [(5_000, 10_000), (20_000, 20_000), (60_000, 30_000)],
)
def test_tier1_psq_clamped_between_10_and_30_pct(
    broken_value: float, expected: float
) -> None:
    pos = _pos(NVDA=broken_value, MSFT=100_000 - broken_value - 40_000, BOXX=40_000)
    plan = build_tier1_plan(pos, 100_000, ["NVDA"], 40_000, PRICES)
    assert plan.legs[0].target_value == pytest.approx(expected)
    assert plan.sell_for_funding_value == 0.0


def test_tier1_existing_psq_deducted_and_empty_plan() -> None:
    pos = _pos(NVDA=50_000, PSQ=10_000, BOXX=40_000)
    plan = build_tier1_plan(pos, 100_000, ["NVDA"], 40_000, PRICES)
    assert plan.legs[0].target_value == pytest.approx(20_000)
    pos2 = _pos(NVDA=50_000, PSQ=30_000, BOXX=20_000)
    plan2 = build_tier1_plan(pos2, 100_000, ["NVDA"], 20_000, PRICES)
    assert plan2.legs == ()


def test_tier1_funding_capped_by_broken_value() -> None:
    # broken=5k（<10% 下限）→ PSQ 目標 10k；現金 0，可減碼上限僅 5k → 新增縮小為 5k
    pos = _pos(NVDA=5_000, MSFT=95_000)
    plan = build_tier1_plan(pos, 100_000, ["NVDA"], 0.0, PRICES)
    assert plan.sell_for_funding_value == pytest.approx(5_000)
    assert plan.legs[0].target_value == pytest.approx(5_000)
    assert plan.boxx_target_value == pytest.approx(0.0)
    assert any("縮小" in n for n in plan.notes)
    assert any("先行減碼" in n for n in plan.notes)
    _check_invariant(plan, pos, 100_000)


def test_tier1_partial_funding() -> None:
    pos = _pos(NVDA=20_000, MSFT=70_000, BOXX=10_000)
    plan = build_tier1_plan(pos, 100_000, ["NVDA"], 10_000, PRICES)
    assert plan.legs[0].target_value == pytest.approx(20_000)
    assert plan.sell_for_funding_value == pytest.approx(10_000)
    assert plan.boxx_target_value == pytest.approx(0.0)


def test_tier2_hedge_hits_target_net_delta() -> None:
    # 現金足夠
    pos = _pos(MSFT=50_000, NVDA=20_000, BOXX=30_000)
    plan = build_tier2_plan(pos, 100_000, [], 30_000, PRICES, {}, TODAY)
    assert plan.mode == "HEDGE"
    assert plan.net_delta_after_pct == pytest.approx(-10.0)
    assert plan.sell_for_funding_value == 0.0
    assert {leg.symbol for leg in plan.legs} == {"SOXS", "SQQQ"}
    _check_invariant(plan, pos, 100_000)
    # 現金不足
    pos2 = _pos(MSFT=80_000, NVDA=15_000, BOXX=5_000)
    plan2 = build_tier2_plan(pos2, 100_000, [], 5_000, PRICES, {}, TODAY)
    assert plan2.net_delta_after_pct == pytest.approx(-10.0)
    assert plan2.sell_for_funding_value > 0
    _check_invariant(plan2, pos2, 100_000)


def test_tier2_hedge_sell_capped_by_kept_long() -> None:
    # 未封頂 x = (2000 + 10000) / 4 = 3000 > kept_long 2000 → 封頂為 2000
    pos = _pos(MSFT=2_000)
    plan = build_tier2_plan(pos, 100_000, [], 0.0, PRICES, {}, TODAY)
    assert plan.mode == "HEDGE"
    assert plan.sell_for_funding_value == pytest.approx(2_000)
    assert sum(leg.target_value for leg in plan.legs) == pytest.approx(2_000)
    assert plan.net_delta_after_pct == pytest.approx(-6.0)
    assert any("現金等價物不足" in n for n in plan.notes)
    _check_invariant(plan, pos, 100_000)


def test_tier2_sell_note_absent_when_frozen() -> None:
    pos = _pos(MSFT=80_000, BOXX=5_000)
    plan = build_tier2_plan(
        pos, 100_000, [], 5_000, PRICES, {}, TODAY, minus3x_frozen_reason="FOMC"
    )
    assert plan.sell_for_funding_value == 0.0
    assert not any("現金等價物不足" in n for n in plan.notes)


def test_tier1_no_broken_no_cash_omits_funding_note() -> None:
    pos = _pos(MSFT=100_000)
    plan = build_tier1_plan(pos, 100_000, ["NVDA"], 0.0, PRICES)
    assert plan.legs == ()
    assert not any("先行減碼" in n for n in plan.notes)


def test_missing_price_nan_inf_treated_as_missing() -> None:
    pos = _pos(MSFT=50_000, BOXX=50_000)
    for bad in (float("nan"), float("inf")):
        plan = build_tier2_plan(pos, 100_000, [], 50_000, {"SQQQ": bad}, {}, TODAY)
        assert plan.legs[0].shares == 0
        assert plan.legs[0].stop_trigger_price == 0.0
        assert any("參考價缺失" in n for n in plan.notes)


def test_tier2_guard_shrinks_to_floor() -> None:
    pos = _pos(NVDA=50_000, PSQ=20_000, BOXX=30_000)
    plan = build_tier2_plan(pos, 100_000, ["NVDA"], 30_000, PRICES, {}, TODAY)
    assert plan.mode == "STOP_SHORT"
    assert plan.net_delta_after_pct == pytest.approx(-120.0)
    assert plan.within_guard
    assert any("守門員" in n for n in plan.notes)
    _check_invariant(plan, pos, 100_000)


def test_tier2_stop_short_cap_deducts_existing_minus3x() -> None:
    pos = _pos(NVDA=50_000, SQQQ=10_000, BOXX=40_000)
    plan = build_tier2_plan(pos, 100_000, ["NVDA"], 40_000, PRICES, {}, TODAY)
    assert sum(leg.target_value for leg in plan.legs) == pytest.approx(30_000)
    assert plan.net_delta_after_pct == pytest.approx(-120.0)


def test_classify_via_sector_benchmark_map(monkeypatch: pytest.MonkeyPatch) -> None:
    from market_analysis import risk_engine

    assert "ZZSEMI" not in rhs.SEMICONDUCTOR_SYMBOLS
    assert classify_hedge_etf("ZZSEMI") == "SQQQ"
    monkeypatch.setitem(risk_engine.SECTOR_BENCHMARK_MAP, "ZZSEMI", "SMH")
    assert classify_hedge_etf("ZZSEMI") == "SOXS"


def test_tier2_stop_short_caps_inverse_at_40_pct() -> None:
    pos = _pos(NVDA=50_000, MSFT=30_000, BOXX=20_000)
    plan = build_tier2_plan(pos, 100_000, ["NVDA", "MSFT"], 20_000, PRICES, {}, TODAY)
    assert plan.mode == "STOP_SHORT"
    total = sum(leg.target_value for leg in plan.legs)
    assert total == pytest.approx(40_000)
    assert plan.net_delta_after_pct >= -120.0 - 1e-6
    assert plan.boxx_target_value == pytest.approx(60_000)
    assert plan.liquidate_symbols == ("MSFT", "NVDA")
    assert plan.liquidate_value == pytest.approx(80_000)
    _check_invariant(plan, pos, 100_000)


def test_tier2_no_longs_no_liquidation_returns_empty_plan() -> None:
    pos = _pos(BOXX=90_000, SQQQ=10_000)
    plan = build_tier2_plan(pos, 100_000, [], 90_000, PRICES, {}, TODAY)
    assert plan.legs == ()
    assert plan.liquidate_symbols == ()
    assert plan.boxx_target_value == pytest.approx(90_000)


def test_tier2_earnings_isolation_halves_leg() -> None:
    pos = _pos(NVDA=30_000, MSFT=30_000, BOXX=40_000)
    base = build_tier2_plan(pos, 100_000, [], 40_000, PRICES, {}, TODAY)
    ed = {"NVDA": date(2026, 11, 13)}
    plan = build_tier2_plan(pos, 100_000, [], 40_000, PRICES, ed, TODAY)
    b = {leg.symbol: leg for leg in base.legs}
    p = {leg.symbol: leg for leg in plan.legs}
    assert p["SOXS"].target_value == pytest.approx(b["SOXS"].target_value * 0.5)
    assert p["SOXS"].earnings_reduced and p["SOXS"].earnings_symbols == ("NVDA",)
    assert not p["SQQQ"].earnings_reduced
    assert plan.boxx_target_value > base.boxx_target_value
    _check_invariant(plan, pos, 100_000)


def test_tier2_macro_freeze_zeroes_minus3x_legs() -> None:
    pos = _pos(NVDA=30_000, MSFT=30_000, BOXX=40_000)
    plan = build_tier2_plan(
        pos,
        100_000,
        ["NVDA"],
        40_000,
        PRICES,
        {},
        TODAY,
        minus3x_frozen_reason="FOMC 前",
    )
    assert plan.legs == ()
    assert plan.minus3x_frozen_reason == "FOMC 前"
    assert any("FOMC 前" in n for n in plan.notes)
    assert plan.liquidate_symbols == ("NVDA",)
    _check_invariant(plan, pos, 100_000)


def test_missing_price_notes() -> None:
    pos = _pos(MSFT=50_000, BOXX=50_000)
    plan = build_tier2_plan(pos, 100_000, [], 50_000, {}, {}, TODAY)
    assert plan.legs[0].shares == 0 and plan.legs[0].stop_trigger_price == 0.0
    assert any("參考價缺失" in n for n in plan.notes)


def test_stop_trigger_and_shares() -> None:
    pos = _pos(MSFT=50_000, BOXX=50_000)
    plan = build_tier2_plan(pos, 100_000, [], 50_000, PRICES, {}, TODAY)
    leg = plan.legs[0]
    assert leg.stop_trigger_price == round(10.0 * 0.93, 2)
    assert leg.shares == int(leg.target_value // 10.0)


def _cases() -> list[tuple[str, str, list[HedgePosition], float, float]]:
    return [
        (
            "tier1",
            "TIER1_PSQ",
            _pos(NVDA=40_000, MSFT=40_000, BOXX=20_000),
            100_000,
            20_000,
        ),
        ("hedge", "HEDGE", _pos(MSFT=80_000, NVDA=15_000, BOXX=5_000), 100_000, 5_000),
        (
            "stop",
            "STOP_SHORT",
            _pos(NVDA=50_000, MSFT=30_000, BOXX=20_000),
            100_000,
            20_000,
        ),
    ]


@pytest.mark.parametrize("kind,mode,pos,nav,cash", _cases())
def test_all_plans_respect_cash_account_invariant(
    kind: str, mode: str, pos: list[HedgePosition], nav: float, cash: float
) -> None:
    if kind == "tier1":
        plan = build_tier1_plan(pos, nav, ["NVDA"], cash, PRICES)
    elif kind == "hedge":
        plan = build_tier2_plan(pos, nav, [], cash, PRICES, {}, TODAY)
    else:
        plan = build_tier2_plan(pos, nav, ["NVDA", "MSFT"], cash, PRICES, {}, TODAY)
    _check_invariant(plan, pos, nav)
    assert plan.mode == mode


def _redline(
    vix: float | None = 25.0,
    vts: float | None = 1.05,
    spot: float | None = 98.0,
    flip: float | None = 100.0,
) -> str | None:
    return rhs.minus3x_activation_block_reason(vix, vts, spot, flip)


def test_redline_all_pass_returns_none() -> None:
    assert _redline() is None


@pytest.mark.parametrize(
    ("kwargs", "blocked"),
    [
        ({"vix": 21.99}, True),
        ({"vix": 22.0}, False),
        ({"vts": 1.0}, True),
        ({"vts": 1.01}, False),
        ({"spot": 99.01}, True),
        ({"spot": 99.0}, False),
    ],
)
def test_redline_boundaries(kwargs: dict[str, float], blocked: bool) -> None:
    reason = _redline(**kwargs)
    assert (reason is not None) is blocked
    if reason is not None:
        assert reason.startswith("−3x 啟動紅線未達：")


def test_redline_unknown_inputs_fail_closed() -> None:
    assert "VIX 未知" in (_redline(vix=None) or "")
    assert "VTS 未知" in (_redline(vts=None) or "")
    assert "未知" in (_redline(spot=None) or "")
    assert "未知" in (_redline(flip=None) or "")
    assert "未知" in (_redline(spot=0.0) or "")
    assert "未知" in (_redline(flip=-1.0) or "")


def test_redline_lists_all_failures_with_values() -> None:
    reason = _redline(vix=21.4, vts=1.0, spot=99.4, flip=100.0) or ""
    assert "VIX 21.40 < 22.00" in reason
    assert "VTS 1.00 ≤ 1.00" in reason
    assert "SPY 低於 Flip 0.60%（需 ≥ 1.00%）" in reason
