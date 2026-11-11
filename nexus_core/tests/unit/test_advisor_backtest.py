"""MQA 機械化回測：BS 定價、指標、無前視、事件封鎖、左右側規則、選擇權生命週期與帳務不變式
（全部使用合成資料，不連網）。"""

from __future__ import annotations

import math
from datetime import date
from typing import Any, Optional

import numpy as np
import pandas as pd
import pytest

from calibration.advisor_backtest import (
    COUNTER_KEYS,
    AdvisorParams,
    AdvisorResult,
    bs_delta,
    bs_price,
    earnings_positions,
    iv_percentile,
    iv_proxy,
    rsi_wilder,
    simulate_advisor,
    strike_for_delta,
)

DAYS = pd.bdate_range("2024-06-03", periods=420)
I0 = 300
START = DAYS[I0].strftime("%Y-%m-%d")
END = DAYS[-1].strftime("%Y-%m-%d")


def _day(k: int) -> pd.Timestamp:
    """起始日後第 k 個交易日。"""
    return DAYS[I0 + k]


def _walk(
    seed: int, drift: float, vol: float, p0: float = 100.0, n: int = len(DAYS)
) -> np.ndarray:
    rng = np.random.default_rng(seed)
    return p0 * np.exp(np.cumsum(drift + vol * rng.standard_normal(n)))


def _frames(
    paths: dict[str, np.ndarray],
) -> tuple[pd.DataFrame, pd.DataFrame]:
    closes = pd.DataFrame(paths, index=DAYS)
    opens = closes.shift(1)
    opens.iloc[0] = closes.iloc[0]
    return opens, closes


def _cash(rate: float = 1e-4) -> pd.Series:
    return pd.Series((1.0 + rate) ** np.arange(len(DAYS)), index=DAYS)


def _vix(level: float = 20.0) -> pd.Series:
    return pd.Series(level, index=DAYS)


def _run(
    paths: dict[str, np.ndarray],
    universe: tuple[str, ...],
    vix: Optional[pd.Series] = None,
    earnings: Optional[dict[str, list[date]]] = None,
    fomc: tuple[date, ...] = (),
    end: str = END,
    **kw: Any,
) -> AdvisorResult:
    opens, closes = _frames(paths)
    params = AdvisorParams(universe=universe, **kw)
    return simulate_advisor(
        opens,
        closes,
        _cash(),
        vix if vix is not None else _vix(),
        START,
        end,
        params,
        earnings or {},
        fomc,
    )


def _uptrend(seed: int = 1) -> dict[str, np.ndarray]:
    return {
        "AAA": _walk(seed, 0.004, 0.002),
        "BBB": _walk(seed + 1, 0.003, 0.002, 80.0),
        "SPY": _walk(seed + 2, 0.002, 0.001, 400.0),
    }


def _trades(
    res: AdvisorResult, kind: str, symbol: Optional[str] = None
) -> list[dict[str, Any]]:
    return [
        t
        for t in res.trades
        if t["kind"] == kind and (symbol is None or t["symbol"] == symbol)
    ]


# ---------------------------------------------------------------------------
# Black-Scholes 與指標
# ---------------------------------------------------------------------------


def test_put_call_parity() -> None:
    s, k, t, r, sigma = 100.0, 95.0, 35 / 365, 0.04, 0.3
    c = bs_price(s, k, t, r, sigma, "C")
    p = bs_price(s, k, t, r, sigma, "P")
    assert abs((c - p) - (s - k * math.exp(-r * t))) < 1e-6


def test_strike_for_delta_matches_target() -> None:
    s, t, r, sigma = 100.0, 35 / 365, 0.04, 0.35
    kp = strike_for_delta(s, t, r, sigma, -0.15, "P")
    kc = strike_for_delta(s, t, r, sigma, 0.25, "C")
    assert abs(bs_delta(s, kp, t, r, sigma, "P") + 0.15) < 1e-4
    assert abs(bs_delta(s, kc, t, r, sigma, "C") - 0.25) < 1e-4
    assert kp < s < kc
    with pytest.raises(ValueError):
        strike_for_delta(s, 0.0, r, sigma, -0.15, "P")


def test_bs_expiry_is_intrinsic() -> None:
    assert bs_price(90.0, 100.0, 0.0, 0.04, 0.3, "P") == 10.0
    assert bs_price(110.0, 100.0, 0.0, 0.04, 0.3, "P") == 0.0
    assert bs_price(110.0, 100.0, 0.0, 0.04, 0.3, "C") == 10.0


def test_rsi_wilder_matches_hand_computation() -> None:
    # 週期 3：漲跌 +1,+1,-1,+2,-1,+2。種子 = 前 3 個漲跌的平均，其後 (n-1)/n 平滑。
    close = pd.Series([10.0, 11.0, 12.0, 11.0, 13.0, 12.0, 14.0])
    r = rsi_wilder(close, 3)
    assert r.iloc[:3].isna().all()
    assert r.iloc[3] == pytest.approx(66.6667, abs=1e-3)  # 2/3 對 1/3
    assert r.iloc[4] == pytest.approx(83.3333, abs=1e-3)  # 1.1111 對 0.2222
    assert r.iloc[5] == pytest.approx(60.6061, abs=1e-3)  # 0.7407 對 0.4815
    assert r.iloc[6] == pytest.approx(78.3333, abs=1e-3)  # 1.1605 對 0.3210
    up = pd.Series(np.arange(1.0, 20.0))
    assert rsi_wilder(up, 14).iloc[-1] == 100.0
    assert rsi_wilder(up, 14).iloc[:14].isna().all()


def test_iv_proxy_and_percentile() -> None:
    idx = pd.RangeIndex(300)
    rv = pd.Series(0.1, index=idx)
    vix = pd.Series(25.0, index=idx)
    rv_m = pd.Series(0.12, index=idx)
    assert iv_proxy(rv, vix, rv_m).iloc[0] == pytest.approx(0.1 + 0.13)
    # 市場 RV 高於 VIX 時不扣分，且有 5% 下限
    low = iv_proxy(pd.Series([0.0]), pd.Series([10.0]), pd.Series([0.5]))
    assert low.iloc[0] == 0.05
    iv = pd.Series(np.arange(300, dtype=float))
    ivp = iv_percentile(iv)
    assert ivp.iloc[:125].isna().all()  # 觀測不足 126 筆
    assert ivp.iloc[125] == 100.0  # 遞增序列中今日永遠最高
    flat = iv_percentile(pd.Series(0.3, index=idx))
    assert flat.iloc[200] == 100.0  # 含自身，≤ 今日者全部


def test_earnings_positions_exit_and_reentry() -> None:
    e = DAYS[310].date()
    (exit_pos, epos, block_end), *_ = earnings_positions(DAYS, [e], 2, 1)
    assert epos == 310 and exit_pos == 308 and block_end == 310
    # 資料之後的財報日以工作日外推
    future = DAYS[-1].date() + pd.Timedelta(days=7)
    (ep, e2, _), *_ = earnings_positions(DAYS, [future], 2, 1)
    assert e2 == len(DAYS) - 1 + 5 and ep == e2 - 2


# ---------------------------------------------------------------------------
# 無前視
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("k", [1, 2, 6])
def test_no_lookahead_open_decisions_ignore_same_day_close(k: int) -> None:
    uni = ("AAA", "BBB")
    paths = _uptrend()
    base = _run(paths, uni)
    opens, closes = _frames(paths)
    day = _day(k)
    perturbed = closes.copy()
    perturbed.loc[day, ["AAA", "BBB"]] *= 0.5  # 第 D+1 日收盤大幅改變，開盤價不變
    alt = simulate_advisor(
        opens,
        perturbed,
        _cash(),
        _vix(),
        START,
        END,
        AdvisorParams(universe=uni),
        {},
        (),
    )

    def at_open(res: AdvisorResult) -> list[tuple[Any, ...]]:
        return [
            (t["kind"], t["symbol"], round(t.get("qty", 0.0), 9))
            for t in res.trades
            if t["phase"] == "open" and t["day"] == day
        ]

    assert at_open(base) == at_open(alt)
    if k <= 2:
        assert at_open(base), "測試前提：該日開盤應有成交"


# ---------------------------------------------------------------------------
# FOMC 與財報封鎖
# ---------------------------------------------------------------------------


def test_fomc_blocks_entry_at_open() -> None:
    uni = ("AAA",)
    paths = _uptrend()
    free = _run(paths, uni, hedge_enabled=False, options_enabled=False)
    first = [
        t for t in free.trades if t["kind"] == "buy_spot_right" and t["phase"] == "open"
    ]
    assert first and first[0]["day"] == _day(1)

    blocked = _run(
        paths,
        uni,
        fomc=(_day(1).date(),),
        hedge_enabled=False,
        options_enabled=False,
    )
    buys = [t for t in blocked.trades if t["kind"] == "buy_spot_right"]
    assert blocked.counters["fomc_blocked"] >= 1
    assert buys and buys[0]["day"] == _day(2)  # 隔日才進場
    assert not [t for t in buys if t["day"] == _day(1)]


def test_earnings_exit_at_close_and_late_reentry() -> None:
    uni = ("AAA",)
    e = _day(20).date()
    res = _run(
        _uptrend(),
        uni,
        earnings={"AAA": [e]},
        initial_state="INVESTED",
        hedge_enabled=False,
        options_enabled=False,
    )
    sells = [t for t in _trades(res, "sell_spot", "AAA")]
    assert sells and sells[0]["phase"] == "close" and sells[0]["day"] == _day(18)
    assert res.counters["earnings_exits"] == 1
    buys = _trades(res, "buy_spot_right", "AAA")
    assert buys
    # 財報日 E = 第 20 日；E+1（第 21 日）收盤才可有訊號 → 最早第 22 日開盤進場
    assert min(t["day"] for t in buys) == _day(22)
    assert not [t for t in buys if _day(18) <= t["day"] < _day(22)]


def test_earnings_exit_options_only_keeps_spot() -> None:
    uni = ("AAA",)
    res = _run(
        _uptrend(),
        uni,
        earnings={"AAA": [_day(20).date()]},
        initial_state="INVESTED",
        hedge_enabled=False,
        earnings_exit_spot=False,
    )
    assert not _trades(res, "sell_spot", "AAA")
    assert any(t["kind"] == "buyback_cc" and t["day"] == _day(18) for t in res.trades)
    assert res.counters["earnings_exits"] == 1


# ---------------------------------------------------------------------------
# 左側規則
# ---------------------------------------------------------------------------


def _decline_paths(_unused: Optional[int] = None) -> dict[str, np.ndarray]:
    """起始日起每日 −0.5% 的持續下跌：RSI 立刻歸零，跌幅逐步達到第二批與選擇權被測試的區間。"""
    aaa = np.full(len(DAYS), 100.0)
    aaa[I0:] = 100.0 * 0.995 ** (np.arange(len(DAYS) - I0) + 1)
    return {"AAA": aaa, "SPY": _walk(5, 0.001, 0.001, 400.0)}


def _time_stop_paths() -> dict[str, np.ndarray]:
    """第 0、1 日各跌 1%（RSI 歸零、收盤低於 tier1 參考價但未達 −5%），之後持平，
    第 30 日急殺 12%（若未暫停就會觸發第二批）。"""
    aaa = np.full(len(DAYS), 100.0)
    for k in range(I0, len(DAYS)):
        step = k - I0
        aaa[k] = 100.0 * 0.99 ** min(step + 1, 2) * (0.88 if step >= 30 else 1.0)
    return {"AAA": aaa, "SPY": _walk(5, 0.001, 0.001, 400.0)}


def test_left_tier_sizes_are_15_and_25_percent_of_budget() -> None:
    res = _run(
        _decline_paths(None),
        ("AAA",),
        options_enabled=False,
        hedge_enabled=False,
        max_loss_nav_pct=1.0,
        left_account_cap=1.0,
    )
    buys = _trades(res, "buy_spot_left", "AAA")
    assert res.counters["tier1"] == 1 and res.counters["tier2"] == 1
    assert len(buys) == 2
    # N=1，B = 前一日淨值 ≈ 100,000（全現金，僅現貨左側部位的浮虧）
    notional = [t["qty"] * t["px"] for t in buys]
    assert notional[0] == pytest.approx(15_000, rel=0.01)
    assert notional[1] == pytest.approx(25_000, rel=0.03)


def test_left_account_cap_never_exceeded_and_blocks_tier2() -> None:
    res = _run(
        _decline_paths(None),
        ("AAA",),
        options_enabled=False,
        hedge_enabled=False,
        max_loss_nav_pct=1.0,
    )
    assert res.counters["tier1"] == 1
    assert res.counters["tier2"] == 0
    assert res.counters["skipped_left_cap"] >= 1
    logs = _trades(res, "left_exposure")
    assert logs
    for t in logs:
        assert t["exposure"] <= 0.40 * t["nav_ref"] + 1e-6


def test_left_symbol_cap_blocks_tier2() -> None:
    res = _run(
        _decline_paths(None),
        ("AAA",),
        options_enabled=False,
        hedge_enabled=False,
        max_loss_nav_pct=1.0,
        left_account_cap=1.0,
        left_symbol_cap=0.30,
    )
    assert res.counters["tier1"] == 1 and res.counters["tier2"] == 0
    assert res.counters["skipped_left_cap"] >= 1


def test_time_stop_pauses_tier2() -> None:
    res = _run(
        _time_stop_paths(),
        ("AAA",),
        options_enabled=False,
        hedge_enabled=False,
        max_loss_nav_pct=1.0,
        left_account_cap=1.0,
    )
    assert res.counters["tier1"] == 1
    assert res.counters["time_stop_pauses"] == 1
    assert res.counters["tier2"] == 0  # 暫停後即使再跌破 −5% 也不再加碼


def test_hard_loss_stop_sells_left_spot() -> None:
    res = _run(
        _decline_paths(None),
        ("AAA",),
        options_enabled=False,
        hedge_enabled=False,
        left_account_cap=1.0,
        max_loss_nav_pct=0.005,
    )
    assert res.counters["loss_stops"] >= 1
    assert _trades(res, "sell_spot", "AAA")


# ---------------------------------------------------------------------------
# 選擇權生命週期
# ---------------------------------------------------------------------------


def _high_ivp_vix() -> pd.Series:
    v = pd.Series(15.0, index=DAYS)
    v.iloc[I0:] = 35.0  # 起始日起波動飆升 → IVP 100，改賣 CSP
    return v


def test_csp_assignment_converts_to_shares_and_opens_cc() -> None:
    res = _run(
        _decline_paths(None),
        ("AAA",),
        vix=_high_ivp_vix(),
        hedge_enabled=False,
        roll_dte=0,
    )
    assert res.counters["csp_opened"] >= 1
    assigned = _trades(res, "csp_assigned", "AAA")
    assert assigned and res.counters["csp_assigned"] >= 1
    a = assigned[0]
    ccs = [
        t
        for t in _trades(res, "open_cc", "AAA")
        if t["day"] == a["day"] and t["phase"] == "close"
    ]
    assert ccs and ccs[0]["qty"] == pytest.approx(a["qty"])
    strikes = [t["strike"] for t in _trades(res, "open_csp", "AAA")]
    assert any(a["strike"] == pytest.approx(k) for k in strikes)


def test_itm_cc_is_called_away_at_strike() -> None:
    aaa = 100.0 * 1.012 ** np.arange(len(DAYS))
    res = _run(
        {"AAA": aaa, "SPY": _walk(5, 0.002, 0.001, 400.0)},
        ("AAA",),
        initial_state="INVESTED",
        hedge_enabled=False,
        roll_dte=0,
    )
    assert res.counters["cc_opened"] >= 1
    away = _trades(res, "cc_called_away", "AAA")
    assert away and res.counters["cc_called_away"] >= 1
    opened = [t["strike"] for t in _trades(res, "open_cc", "AAA")]
    assert any(away[0]["strike"] == pytest.approx(k) for k in opened)
    assert away[0]["qty"] > 0


def test_cc_take_profit_at_fifty_percent() -> None:
    # 價格幾乎不動：OTM 的 Call 隨時間價值衰減，跌到收取權利金的一半以下即買回
    aaa = 100.0 * 1.0002 ** np.arange(len(DAYS))
    res = _run(
        {"AAA": aaa, "SPY": _walk(5, 0.002, 0.001, 400.0)},
        ("AAA",),
        initial_state="INVESTED",
        hedge_enabled=False,
        roll_dte=0,
    )
    assert res.counters["cc_tp"] >= 1
    tp = [t for t in _trades(res, "buyback_cc", "AAA") if t["phase"] == "close"]
    assert tp and _day(3) < tp[0]["day"] < _day(34)


def test_csp_rolls_when_tested_inside_roll_window() -> None:
    res = _run(
        _decline_paths(),
        ("AAA",),
        vix=_high_ivp_vix(),
        hedge_enabled=False,
        max_loss_nav_pct=1.0,
    )
    assert res.counters["rolls"] >= 1
    assert res.counters["csp_opened"] >= 2
    assert res.counters["shorts"] == 0


def test_protective_put_hedge_opens_and_settles() -> None:
    spy = np.concatenate(
        [np.full(I0 - 5, 400.0), 400.0 * 0.99 ** np.arange(len(DAYS) - I0 + 5)]
    )
    res = _run(
        {"AAA": _walk(1, 0.003, 0.002), "SPY": spy},
        ("AAA",),
        initial_state="INVESTED",
    )
    assert res.counters["hedges"] >= 1
    assert _trades(res, "open_hedge")
    assert _trades(res, "settle_hedge")


# ---------------------------------------------------------------------------
# 帳務不變式
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("state", ["CASH", "INVESTED"])
def test_cash_non_negative_and_never_short(state: str) -> None:
    paths = {
        "AAA": _walk(11, 0.0005, 0.02),
        "BBB": _walk(12, 0.0003, 0.025, 60.0),
        "CCC": _walk(13, -0.0005, 0.03, 120.0),
        "SPY": _walk(14, 0.0004, 0.01, 400.0),
    }
    res = _run(
        paths,
        ("AAA", "BBB", "CCC"),
        vix=_high_ivp_vix(),
        initial_state=state,
    )
    assert res.min_cash >= -1e-6
    assert res.counters["shorts"] == 0
    assert set(res.counters) == set(COUNTER_KEYS)
    assert (res.nav > 0).all()
    assert (res.equity_exposure >= -1e-9).all()


def test_invested_state_equals_equal_weight_on_day_one() -> None:
    uni = ("AAA", "BBB")
    paths = _uptrend()
    res = _run(
        paths,
        uni,
        initial_state="INVESTED",
        options_enabled=False,
        hedge_enabled=False,
        spot_cost_rate=0.0,
    )
    opens, closes = _frames(paths)
    i0 = I0
    expected = sum((100_000.0 / 2) * closes[s].iat[i0] / opens[s].iat[i0] for s in uni)
    assert res.nav.iloc[0] == pytest.approx(expected, rel=1e-9)
    assert res.equity_exposure.iloc[0] == pytest.approx(1.0, abs=1e-9)


def test_cash_start_is_all_cash_on_day_one() -> None:
    res = _run(_uptrend(), ("AAA", "BBB"))
    assert res.nav.iloc[0] == pytest.approx(100_000.0, rel=1e-9)
    assert res.equity_exposure.iloc[0] == 0.0


def test_requires_market_symbol_and_enough_days() -> None:
    opens, closes = _frames({"AAA": _walk(1, 0.0, 0.01)})
    with pytest.raises(ValueError):
        simulate_advisor(opens, closes, _cash(), _vix(), START, END)


def test_open_options_are_marked_into_option_pnl() -> None:
    aaa = 100.0 * 1.0002 ** np.arange(len(DAYS))
    res = _run(
        {"AAA": aaa, "SPY": _walk(5, 0.002, 0.001, 400.0)},
        ("AAA",),
        end=_day(10).strftime("%Y-%m-%d"),
        initial_state="INVESTED",
        hedge_enabled=False,
    )
    parts = res.option_pnl_parts
    assert res.counters["cc_opened"] >= 1 and res.counters["cc_tp"] == 0
    assert parts["buyback"] == 0.0 and parts["settle"] == 0.0
    assert parts["open_mtm"] < 0  # 仍開著的空方 Call 是負債
    assert res.option_pnl == pytest.approx(sum(parts.values()))
    assert res.option_pnl == pytest.approx(parts["premium"] + parts["open_mtm"])
    assert res.option_pnl < parts["premium"]


def test_spot_round_trip_costs_reduce_nav() -> None:
    uni = ("AAA",)
    kw: dict[str, Any] = dict(
        earnings={"AAA": [_day(7).date()]},
        initial_state="INVESTED",
        options_enabled=False,
        hedge_enabled=False,
    )
    free = _run(_uptrend(), uni, spot_cost_rate=0.0, **kw)
    paid = _run(_uptrend(), uni, spot_cost_rate=0.0015, **kw)
    notional = sum(
        t["qty"] * t["px"]
        for t in paid.trades
        if t["kind"]
        in (
            "initial_buy",
            "buy_spot_right",
            "buy_spot_left",
            "sell_spot",
            "sell_spot_cover",
        )
    )
    assert paid.spot_costs == pytest.approx(0.0015 * notional)
    assert free.spot_costs == 0.0
    assert paid.counters["earnings_exits"] == 1  # 至少一次完整來回
    assert paid.nav.iloc[-1] < free.nav.iloc[-1]
    # 成本本身加上少投入資金錯過的上漲（強勢上升趨勢），差額至少等於成本、不會超過數倍
    gap = free.nav.iloc[-1] - paid.nav.iloc[-1]
    assert paid.spot_costs <= gap < 3.0 * paid.spot_costs


def test_pre_exit_close_signal_does_not_enter_before_earnings_exit() -> None:
    # 財報日 = 第 3 日 → 出場日 = 第 1 日；封鎖自第 0 日收盤起，第 1 日開盤不得進場
    res = _run(
        _uptrend(),
        ("AAA",),
        earnings={"AAA": [_day(3).date()]},
        options_enabled=False,
        hedge_enabled=False,
    )
    buys = _trades(res, "buy_spot_right", "AAA")
    assert buys
    # E+1（第 4 日）收盤才可有訊號 → 最早第 5 日開盤
    assert min(t["day"] for t in buys) == _day(5)


def test_unaffordable_roll_counted_once_per_option() -> None:
    aaa = 100.0 * 1.012 ** np.arange(len(DAYS))
    res = _run(
        {"AAA": aaa, "SPY": _walk(5, 0.002, 0.001, 400.0)},
        ("AAA",),
        initial_state="INVESTED",
        hedge_enabled=False,
    )
    assert res.counters["roll_skipped"] >= 1
    assert res.counters["roll_skipped"] <= res.counters["cc_opened"]
