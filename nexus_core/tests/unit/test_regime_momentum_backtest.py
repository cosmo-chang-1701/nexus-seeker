"""大盤三態切換 + 動能輪動回測：狀態判定、無前視、point-in-time 選股、回落出場、
BOXX／BIL 銜接與等權對照組的上市後加入規則（全部使用合成資料）。"""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from calibration.regime_momentum_backtest import (
    CORE_SYMBOL,
    RegimeMomentumParams,
    build_cash_index,
    chain_returns,
    classify_regime_raw,
    confirm_regime,
    equal_weight_pit,
    momentum_scores,
    rank_top,
    regime_switch_stats,
    simulate,
)

DAYS = pd.bdate_range("2020-01-01", periods=120)


def _params(**kw: object) -> RegimeMomentumParams:
    base = dict(
        top_n=2,
        defensive_top_n=1,
        trailing_stop=0.25,
        confirm_days=3,
        momentum_lookback=10,
        momentum_skip=2,
        sma_fast=3,
        sma_slow=5,
        cost_rate=0.0,
        fallback_cash_rate=0.0,
        universe=("AAA", "BBB", "CCC"),
        defensive=("DDD",),
    )
    base.update(kw)
    return RegimeMomentumParams(**base)  # type: ignore[arg-type]


def _panel(overrides: dict[str, np.ndarray] | None = None) -> pd.DataFrame:
    n = len(DAYS)
    t = np.arange(n, dtype=float)
    data = {
        "SPY": 100 + t,  # 持續上漲 → GOOD
        "AAA": 50 * 1.02**t,
        "BBB": 50 * 1.01**t,
        "CCC": 50 * 1.005**t,
        "DDD": 50 + 0 * t,
        CORE_SYMBOL: 100 + t,
    }
    if overrides:
        data.update(overrides)
    return pd.DataFrame(data, index=DAYS)


def _cash() -> pd.Series:
    return pd.Series(1.0, index=DAYS)


# ---------------------------------------------------------------------------
# 狀態判定
# ---------------------------------------------------------------------------


def test_classify_regime_three_states() -> None:
    close = pd.Series([10, 10, 10, 10, 10, 20, 1, 1, 1, 1, 30], dtype=float)
    raw = classify_regime_raw(close, fast=2, slow=5)
    assert raw.iloc[:4].isna().all(), "均線資料不足時不給狀態"
    # 第 5 日：收盤 20 > SMA5，SMA2 (15) > SMA5 (12) → GOOD
    assert raw.iloc[5] == "GOOD"
    # 第 6 日：收盤 1 < SMA5 (10.2)，但 SMA2 (10.5) > SMA5 → 只成立一個 → WEAK
    assert raw.iloc[6] == "WEAK"
    # 第 9 日：收盤 1、SMA2 1、SMA5 4.8 → 兩者皆不成立 → BAD
    assert raw.iloc[9] == "BAD"


def test_confirm_regime_requires_consecutive_days() -> None:
    raw = pd.Series(
        ["GOOD", "GOOD", "WEAK", "WEAK", "GOOD", "WEAK", "WEAK", "WEAK", "BAD"]
    )
    out = confirm_regime(raw, 3).tolist()
    # 中斷的 WEAK 不生效；連續 3 日 WEAK 在第 3 日生效
    assert out == [
        "GOOD",
        "GOOD",
        "GOOD",
        "GOOD",
        "GOOD",
        "GOOD",
        "GOOD",
        "WEAK",
        "WEAK",
    ]


def test_confirm_regime_one_day_follows_raw() -> None:
    raw = pd.Series(["GOOD", "BAD", "WEAK"])
    assert confirm_regime(raw, 1).tolist() == ["GOOD", "BAD", "WEAK"]


def test_regime_switch_stats_counts_whipsaw() -> None:
    reg = pd.Series(
        ["GOOD"] * 5 + ["WEAK"] * 3 + ["GOOD"] * 30 + ["BAD"] * 30 + ["WEAK"]
    )
    st = regime_switch_stats(reg, whipsaw_days=20)
    assert st["switches"] == 4
    assert st["whipsaws"] == 1  # GOOD→WEAK→GOOD 在 3 日內切回


# ---------------------------------------------------------------------------
# 動能與 point-in-time
# ---------------------------------------------------------------------------


def test_momentum_requires_full_history_point_in_time() -> None:
    close = _panel()[["AAA"]].copy()
    close.loc[DAYS[:30], "AAA"] = np.nan  # 第 30 日才上市
    scores = momentum_scores(close, lookback=10, skip=2)
    first = scores["AAA"].first_valid_index()
    # 需要 lookback+1 = 11 筆有效收盤：上市第 11 個交易日才有分數
    assert first == DAYS[30 + 10]


def test_rank_top_ignores_nan_and_is_deterministic() -> None:
    s = pd.Series({"B": 0.5, "A": 0.5, "C": np.nan, "D": 0.1})
    assert rank_top(s, 2) == ["A", "B"]
    assert rank_top(s, 10) == ["A", "B", "D"]


def test_unlisted_symbol_never_picked() -> None:
    px = _panel({"CCC": np.full(len(DAYS), np.nan)})
    px.loc[DAYS[100] :, "CCC"] = 50 * 1.5 ** np.arange(len(DAYS) - 100)  # 晚上市且暴漲
    res = simulate(
        px,
        px,
        _cash(),
        px["SPY"],
        str(DAYS[20].date()),
        str(DAYS[-1].date()),
        _params(top_n=3),
    )
    bought = {t.symbol for t in res.trades if t.notional > 0}
    ccc_buys = [t for t in res.trades if t.symbol == "CCC" and t.notional > 0]
    # 上市後需累積 11 筆收盤才有動能分數，最早也要在上市第 12 個交易日之後才可能被買進
    assert all(pd.Timestamp(t.day) > DAYS[100 + 10] for t in ccc_buys)
    assert "AAA" in bought


# ---------------------------------------------------------------------------
# 無前視
# ---------------------------------------------------------------------------


def test_no_lookahead_future_prices_do_not_change_past_decisions() -> None:
    px = _panel()
    cut = 70
    res_a = simulate(
        px,
        px,
        _cash(),
        px["SPY"],
        str(DAYS[20].date()),
        str(DAYS[-1].date()),
        _params(),
    )
    alt = px.copy()
    # 改寫第 cut 日（含）之後的一切價格：大盤崩跌、個股排名反轉
    alt.loc[DAYS[cut] :, "SPY"] = 10.0
    alt.loc[DAYS[cut] :, "CCC"] = alt.loc[DAYS[cut] :, "CCC"] * 50
    res_b = simulate(
        alt,
        alt,
        _cash(),
        alt["SPY"],
        str(DAYS[20].date()),
        str(DAYS[-1].date()),
        _params(),
    )
    before = pd.Timestamp(DAYS[cut].date())
    # 第 cut 日開盤的成交價已被改寫，只比較 cut 之前的交易；cut 日的「決策」另以狀態比對
    trades_a = [
        (t.day, t.symbol, round(t.notional, 6))
        for t in res_a.trades
        if pd.Timestamp(t.day) < before
    ]
    trades_b = [
        (t.day, t.symbol, round(t.notional, 6))
        for t in res_b.trades
        if pd.Timestamp(t.day) < before
    ]
    assert trades_a and trades_a == trades_b
    assert res_a.nav.loc[: DAYS[cut - 1]].equals(res_b.nav.loc[: DAYS[cut - 1]])
    # 第 cut 日當天生效的狀態仍以 cut−1 收盤判定
    assert res_a.regime.loc[DAYS[cut]] == res_b.regime.loc[DAYS[cut]]


def test_regime_switch_executes_next_day_after_confirmation() -> None:
    px = _panel()
    crash = 60
    px.loc[DAYS[crash] :, "SPY"] = 1.0  # 第 60 日起跌破均線 → 原始狀態轉 BAD（或 WEAK）
    res = simulate(
        px,
        px,
        _cash(),
        px["SPY"],
        str(DAYS[20].date()),
        str(DAYS[-1].date()),
        _params(),
    )
    raw_change = DAYS[crash]
    # 連續 3 日確認：第 crash+2 日收盤確認，第 crash+3 日開盤執行
    exec_day = DAYS[crash + 3].date()
    switch_trades = [t for t in res.trades if t.reason.startswith("狀態切換 GOOD")]
    assert switch_trades, "應有狀態切換的調整"
    assert min(t.day for t in switch_trades) == exec_day
    assert res.regime.loc[raw_change] == "GOOD"


# ---------------------------------------------------------------------------
# 回落出場
# ---------------------------------------------------------------------------


def test_trailing_stop_uses_high_since_entry() -> None:
    n = len(DAYS)
    aaa = np.array(50 * 1.02 ** np.arange(n))
    peak_day = 50
    aaa[peak_day + 1 :] = aaa[peak_day] * 0.70  # 從持有期間高點回落 30%
    px = _panel({"AAA": aaa})
    res = simulate(
        px,
        px,
        _cash(),
        px["SPY"],
        str(DAYS[20].date()),
        str(DAYS[-1].date()),
        _params(),
    )
    stops = [s for s in res.stops if s.symbol == "AAA"]
    assert stops and stops[0].day == DAYS[peak_day + 1].date()
    assert stops[0].peak == pytest.approx(aaa[peak_day])
    exits = [t for t in res.trades if t.symbol == "AAA" and t.reason == "回落停損"]
    assert exits and exits[0].day == DAYS[peak_day + 2].date(), "次一交易日開盤出場"


def test_trailing_stop_ignores_pre_entry_high() -> None:
    n = len(DAYS)
    # BBB 進場前曾有高點 200，之後回到 50 附近緩漲：進場後不應因進場前高點觸發
    bbb = np.array(50 * 1.012 ** np.arange(n))
    bbb[:15] = 200.0
    px = _panel({"BBB": bbb, "CCC": 50 * 0.99 ** np.arange(n)})
    res = simulate(
        px,
        px,
        _cash(),
        px["SPY"],
        str(DAYS[40].date()),
        str(DAYS[-1].date()),
        _params(),
    )
    assert not [s for s in res.stops if s.symbol == "BBB"]


def test_stop_only_applies_to_universe_members() -> None:
    n = len(DAYS)
    voo = np.array(100.0 + np.arange(n))
    px = _panel({CORE_SYMBOL: voo})
    res = simulate(
        px,
        px,
        _cash(),
        px["SPY"],
        str(DAYS[20].date()),
        str(DAYS[-1].date()),
        _params(),
    )
    assert all(s.symbol in ("AAA", "BBB", "CCC") for s in res.stops)


# ---------------------------------------------------------------------------
# 現金指數與代理串接
# ---------------------------------------------------------------------------


def test_cash_index_splices_bil_then_boxx_then_fallback() -> None:
    cal = pd.bdate_range("2021-01-01", periods=10)
    bil = pd.Series(
        [np.nan, np.nan, 10, 10.1, 10.2, 10.3, 10.4, 10.5, 10.6, 10.7], index=cal
    )
    boxx = pd.Series([np.nan] * 6 + [100, 102, 104, 106], index=cal, dtype=float)
    idx = build_cash_index(cal, boxx, bil, fallback_rate=0.252)  # 每日 0.1%
    r = idx.pct_change()
    assert r.iloc[1] == pytest.approx(0.001)  # 兩者皆無 → 固定利率
    assert r.iloc[3] == pytest.approx(10.1 / 10 - 1)  # BIL
    assert r.iloc[7] == pytest.approx(102 / 100 - 1)  # BOXX 覆蓋 BIL
    assert idx.iloc[0] == 1.0


def test_chain_returns_uses_proxy_before_listing() -> None:
    cal = pd.bdate_range("2021-01-01", periods=5)
    proxy = pd.Series([100, 110, 121, 133.1, 146.41], index=cal)
    primary = pd.Series([np.nan, np.nan, 50, 55, 60.5], index=cal)
    out = chain_returns(primary, proxy)
    assert out.iloc[2:].tolist() == pytest.approx([50, 55, 60.5])
    assert out.iloc[1] == pytest.approx(50 / 1.1)
    assert out.iloc[0] == pytest.approx(50 / 1.21)


# ---------------------------------------------------------------------------
# 對照組 b：等權持有選股池（point-in-time）
# ---------------------------------------------------------------------------


def test_equal_weight_pit_adds_new_listing_only_at_next_annual_rebalance() -> None:
    cal = pd.bdate_range("2020-12-01", "2022-01-31")
    n = len(cal)
    a = pd.Series(100.0, index=cal)
    b = pd.Series(np.nan, index=cal)
    listing = pd.Timestamp("2021-06-01")
    b[cal >= listing] = np.linspace(100, 300, int((cal >= listing).sum()))
    px = pd.DataFrame({"A": a, "B": b})
    nav = equal_weight_pit(px, px, ("A", "B"), "2021-01-01", "2022-01-31", 0.0)
    # 2021 年內只持有 A（價格不變）→ 淨值恆定；B 上市後的上漲在 2021 不反映
    in_2021 = nav[(nav.index >= "2021-01-01") & (nav.index <= "2021-12-31")]
    assert in_2021.max() == pytest.approx(in_2021.min())
    # 2022 年初再平衡後納入 B，B 繼續上漲 → 淨值上升
    in_2022 = nav[nav.index >= "2022-01-01"]
    assert in_2022.iloc[-1] > in_2022.iloc[0]
    assert n > 0
