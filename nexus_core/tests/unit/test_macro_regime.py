"""總經切換：FRED 解析、Sahm 公布延遲、利差警訊可用時點、任一／兩者判定、連續確認與
無前視，以及 simulate_static 的股票部位內部換倉（BOXX 不動）與年度再平衡互動。
全部使用合成資料。"""

from __future__ import annotations

import numpy as np
import pandas as pd

import pytest

from calibration.macro_regime import (
    MacroParams,
    fed_hike_warning,
    macro_states3,
    raw_state3,
    targets_series,
    macro_states,
    parse_fred_csv,
    raw_macro_state,
    sahm_available_dates,
    sahm_warning,
    spread_warning,
    state_episodes,
    tech_share_series,
    whipsaw_count,
)
from calibration.regime_momentum_backtest import CORE_SYMBOL
from calibration.static_allocation_backtest import (
    BUCKET_CASH,
    BUCKET_CORE,
    BUCKET_TECH,
    StaticAllocationParams,
    simulate_static,
)

CAL = pd.bdate_range("2019-12-02", "2021-03-31")


# ---------------------------------------------------------------------------
# FRED 解析與可用時點
# ---------------------------------------------------------------------------


def test_parse_fred_csv_drops_missing_dots() -> None:
    text = "observation_date,BAA10Y\n2020-01-02,2.10\n2020-01-03,.\n2020-01-06,2.30\n"
    s = parse_fred_csv(text)
    assert list(s.index) == [pd.Timestamp("2020-01-02"), pd.Timestamp("2020-01-06")]
    assert s.tolist() == [2.10, 2.30]


def test_sahm_value_available_on_tenth_of_next_month_rolled_forward() -> None:
    # 2020-09 的數值名目上於 2020-10-10（星期六）可用 → 順延到 2020-10-12（星期一）
    obs = pd.DatetimeIndex([pd.Timestamp("2020-09-01"), pd.Timestamp("2020-10-01")])
    avail = sahm_available_dates(obs, CAL, release_day=10)
    assert avail[0] == pd.Timestamp("2020-10-12")
    assert avail[1] == pd.Timestamp("2020-11-10")


def test_sahm_warning_not_usable_before_release() -> None:
    sahm = pd.Series(
        [0.1, 0.8],
        index=pd.DatetimeIndex(
            [pd.Timestamp("2020-02-01"), pd.Timestamp("2020-03-01")]
        ),
    )
    warn = sahm_warning(sahm, CAL, threshold=0.5, release_day=10)
    # 3 月的 0.8 在 4 月 10 日（星期五）前不可用；之前只看得到 2 月的 0.1
    assert warn.loc["2020-04-09"] == 0.0
    assert warn.loc["2020-04-10"] == 1.0
    # 2 月的數值 3 月 10 日才可用：之前完全沒有可用數值
    assert np.isnan(warn.loc["2020-03-09"])
    assert warn.loc["2020-03-10"] == 0.0


def test_spread_warning_multiple_and_strict_next_day_availability() -> None:
    days = pd.bdate_range("2020-01-01", periods=10)
    spread = pd.Series([1.0] * 5 + [2.0] * 5, index=days)
    warn = spread_warning(spread, CAL, ma_days=3, mult=1.2)
    first_jump = days[5]
    # 觀測日當天不可用（沿用前一筆＝未觸發），次一交易日才看得到警訊
    assert warn.loc[first_jump] == 0.0
    assert warn.loc[days[6]] == 1.0
    # 均線暖機不足（前 2 筆）→ 未知
    assert np.isnan(warn.loc[days[1]])


def test_raw_state_any_vs_all_and_unknown() -> None:
    idx = pd.bdate_range("2020-01-01", periods=4)
    a = pd.Series([1.0, 0.0, 1.0, np.nan], index=idx)
    b = pd.Series([0.0, 0.0, 1.0, 0.0], index=idx)
    any_ = raw_macro_state(a, b, "ANY")
    all_ = raw_macro_state(a, b, "ALL")
    assert any_.tolist()[:3] == ["BAD", "GOOD", "BAD"]
    assert all_.tolist()[:3] == ["GOOD", "GOOD", "BAD"]
    assert pd.isna(any_.iloc[3]) and pd.isna(all_.iloc[3])


def test_macro_states_confirmation_and_no_lookahead() -> None:
    # 利差在第 40 個交易日跳升；Sahm 恆為 0（不觸發）
    n = len(CAL)
    spread = pd.Series(np.where(np.arange(n) >= 40, 3.0, 1.0), index=CAL)
    sahm = pd.Series(0.0, index=pd.date_range("2019-06-01", "2021-03-01", freq="MS"))
    p = MacroParams(spread_ma_days=20, spread_mult=1.2, confirm_days=5)
    st = macro_states(spread, sahm, CAL, p)
    raw_bad = st.index[st["raw"] == "BAD"][0]
    conf_bad = st.index[st["confirmed"] == "BAD"][0]
    eff_bad = st.index[st["effective"] == "BAD"][0]
    # 觀測日 = CAL[40]；次一交易日才可用
    assert raw_bad == CAL[41]
    # 需連續 5 日：第 5 天才確認
    assert conf_bad == CAL[45]
    # 開盤採用的狀態 = 前一交易日收盤後確認的狀態
    assert eff_bad == CAL[46]
    assert (
        st["effective"].iloc[1:].to_numpy() == st["confirmed"].iloc[:-1].to_numpy()
    ).all()


def test_tech_share_mapping_and_default_state() -> None:
    eff = pd.Series([np.nan, "GOOD", "BAD"], index=CAL[:3], dtype=object)
    t = tech_share_series(eff, MacroParams())
    assert t.tolist() == [1.0, 1.0, 0.0]


def test_state_episodes_and_whipsaw() -> None:
    s = pd.Series(
        ["GOOD"] * 5 + ["BAD"] * 2 + ["GOOD"] * 5 + ["BAD"] * 10, index=CAL[:22]
    )
    eps = state_episodes(s)
    assert [(e.state, e.days) for e in eps] == [
        ("GOOD", 5),
        ("BAD", 2),
        ("GOOD", 5),
        ("BAD", 10),
    ]
    # 只有「長度 ≤ window 且前後狀態相同」的中段算 whipsaw：BAD 2 日算，GOOD 5 日不算
    assert whipsaw_count(eps, window=3) == 1


# ---------------------------------------------------------------------------
# simulate_static 的總經換倉
# ---------------------------------------------------------------------------


def _flat_panel() -> pd.DataFrame:
    return pd.DataFrame(
        {"AAA": 50.0, "BBB": 40.0, CORE_SYMBOL: 100.0}, index=CAL, dtype=float
    )


def _params(**kw: object) -> StaticAllocationParams:
    base: dict[str, object] = dict(
        equity_share=0.4,
        tech_share=1.0,
        rebalance="ANNUAL",
        cost_rate=0.0,
        universe=("AAA", "BBB"),
    )
    base.update(kw)
    return StaticAllocationParams(**base)  # type: ignore[arg-type]


def test_constant_tech_series_is_bit_identical_to_no_series() -> None:
    t = np.arange(len(CAL), dtype=float)
    px = pd.DataFrame(
        {"AAA": 50 * 1.002**t, "BBB": 40 * 0.999**t, CORE_SYMBOL: 100 + 0.1 * t},
        index=CAL,
    )
    cash = pd.Series(1.0001 ** np.arange(len(CAL)), index=CAL)
    p = _params(cost_rate=0.0015)
    a = simulate_static(px, px, cash, "2019-12-02", "2021-03-31", p)
    b = simulate_static(
        px,
        px,
        cash,
        "2019-12-02",
        "2021-03-31",
        p,
        tech_share_by_day=pd.Series(1.0, index=CAL),
    )
    assert a.nav.equals(b.nav)
    assert b.switch_days == []


def test_switch_swaps_equity_sleeve_and_leaves_boxx_untouched() -> None:
    px = _flat_panel()
    cash = pd.Series(1.0, index=CAL)
    switch_at = CAL[30]
    t = pd.Series(np.where(CAL >= switch_at, 0.0, 1.0), index=CAL)
    res = simulate_static(
        px, px, cash, "2019-12-02", "2021-03-31", _params(), tech_share_by_day=t
    )
    w = res.bucket_weights
    before = w.loc[CAL[29]]
    after = w.loc[switch_at]
    assert before[BUCKET_TECH] > 0.39 and before[BUCKET_CORE] < 1e-12
    assert after[BUCKET_TECH] < 1e-12
    assert abs(after[BUCKET_CORE] - before[BUCKET_TECH]) < 1e-12
    assert abs(after[BUCKET_CASH] - before[BUCKET_CASH]) < 1e-12
    assert res.switch_days == [switch_at]


def test_annual_rebalance_uses_current_macro_share() -> None:
    px = _flat_panel()
    cash = pd.Series(1.0, index=CAL)
    # 2020-06 切到 VOO 且之後一直「不好」：2021 年第一個交易日的再平衡仍維持全 VOO
    t = pd.Series(np.where(CAL >= pd.Timestamp("2020-06-01"), 0.0, 1.0), index=CAL)
    res = simulate_static(
        px, px, cash, "2019-12-02", "2021-03-31", _params(), tech_share_by_day=t
    )
    first_2021 = CAL[CAL.year == 2021][0]
    assert first_2021 in res.rebalance_days
    w = res.bucket_weights.loc[first_2021]
    assert w[BUCKET_TECH] < 1e-12
    assert abs(w[BUCKET_CORE] - 0.4) < 1e-12
    assert abs(w[BUCKET_CASH] - 0.6) < 1e-12


def test_switch_cost_charged_on_turnover() -> None:
    px = _flat_panel()
    cash = pd.Series(1.0, index=CAL)
    t = pd.Series(np.where(CAL >= CAL[30], 0.0, 1.0), index=CAL)
    res = simulate_static(
        px,
        px,
        cash,
        "2019-12-02",
        "2021-03-31",
        _params(cost_rate=0.001),
        tech_share_by_day=t,
    )
    # 價格恆定：淨值變化只來自成本。建倉 40k + 換倉（賣 40k、買 40k）= 120k 成交額
    expected_nav = 100_000.0 * (1 - 0.4 * 0.001) * (1 - 0.8 * 0.001)
    assert abs(res.nav.loc[CAL[30]] - expected_nav) < 1e-6


# ---------------------------------------------------------------------------
# 三態配置
# ---------------------------------------------------------------------------


def test_raw_state3_counts_warnings() -> None:
    idx = pd.bdate_range("2020-01-01", periods=5)
    a = pd.Series([0.0, 1.0, 1.0, 1.0, np.nan], index=idx)
    b = pd.Series([0.0, 0.0, 1.0, 1.0, 0.0], index=idx)
    c = pd.Series([0.0, 0.0, 0.0, 1.0, 0.0], index=idx)
    two = raw_state3([a, b], worst_min=2)
    three = raw_state3([a, b, c], worst_min=2)
    assert two.tolist()[:4] == ["GOOD", "WEAK", "WORST", "WORST"]
    assert three.tolist()[:4] == ["GOOD", "WEAK", "WORST", "WORST"]
    assert pd.isna(two.iloc[4]) and pd.isna(three.iloc[4])


def test_fed_hike_warning_uses_trading_day_lookback_and_next_day_availability() -> None:
    # DFF 含週末的每日序列：2020-03-02 起由 0.1 升到 1.2（+1.1 pp）
    cal_days = pd.date_range("2019-12-01", "2020-06-30", freq="D")
    dff = pd.Series(
        np.where(cal_days >= pd.Timestamp("2020-03-02"), 1.2, 0.1), index=cal_days
    )
    warn = fed_hike_warning(dff, CAL, lookback=20, threshold=1.0)
    # 觀測日 2020-03-02 當天不可用，次一交易日 2020-03-03 才看得到升幅
    assert warn.loc["2020-03-02"] == 0.0
    assert warn.loc["2020-03-03"] == 1.0
    # 20 個交易日後，比較基準也已是 1.2 → 警訊熄滅
    later = CAL[CAL.get_loc(pd.Timestamp("2020-03-03")) + 20]
    assert warn.loc[later] == 0.0
    # 回看不足 → 未知
    assert np.isnan(warn.iloc[5])


def test_macro_states3_requires_dff_for_variant() -> None:
    spread = pd.Series(1.0, index=CAL)
    sahm = pd.Series(0.0, index=pd.date_range("2019-06-01", "2021-03-01", freq="MS"))
    with pytest.raises(ValueError):
        macro_states3(spread, sahm, CAL, MacroParams(include_fed_hike=True))


def test_macro_states3_effective_is_previous_confirmed() -> None:
    n = len(CAL)
    spread = pd.Series(np.where(np.arange(n) >= 40, 3.0, 1.0), index=CAL)
    sahm = pd.Series(0.0, index=pd.date_range("2019-06-01", "2021-03-01", freq="MS"))
    st = macro_states3(spread, sahm, CAL, MacroParams(spread_ma_days=20))
    assert st.index[st["confirmed"] == "WEAK"][0] == CAL[45]
    assert st.index[st["effective"] == "WEAK"][0] == CAL[46]
    assert "WORST" not in set(st["confirmed"].dropna())


def test_targets_series_states_and_equity_scale() -> None:
    eff = pd.Series([np.nan, "GOOD", "WEAK", "WORST"], index=CAL[:4], dtype=object)
    full = targets_series(eff)
    assert full.to_numpy().tolist() == [
        [1.0, 0.0, 0.0],
        [1.0, 0.0, 0.0],
        [0.0, 1.0, 0.0],
        [0.0, 0.0, 1.0],
    ]
    half = targets_series(eff, equity_scale=0.5)
    assert half.to_numpy().tolist() == [
        [0.5, 0.0, 0.5],
        [0.5, 0.0, 0.5],
        [0.0, 0.5, 0.5],
        [0.0, 0.0, 1.0],
    ]


def _targets(states: list[str]) -> pd.DataFrame:
    return targets_series(pd.Series(states, index=CAL[: len(states)], dtype=object))


def test_state_change_triggers_full_rebalance_next_open() -> None:
    px = _flat_panel()
    cash = pd.Series(1.0, index=CAL)
    states = ["GOOD"] * 30 + ["WEAK"] * 20 + ["WORST"] * (len(CAL) - 50)
    res = simulate_static(
        px,
        px,
        cash,
        "2019-12-02",
        "2021-03-31",
        _params(equity_share=1.0),
        targets_by_day=_targets(states),
    )
    w = res.bucket_weights
    assert w.loc[CAL[29], BUCKET_TECH] > 0.999
    assert w.loc[CAL[30], BUCKET_CORE] > 0.999
    assert w.loc[CAL[50], BUCKET_CASH] > 0.999
    assert res.switch_days == [CAL[30], CAL[50]]


def test_annual_rebalance_keeps_current_state_targets() -> None:
    px = _flat_panel()
    cash = pd.Series(1.0, index=CAL)
    states = ["GOOD"] * 30 + ["WEAK"] * (len(CAL) - 30)
    res = simulate_static(
        px,
        px,
        cash,
        "2019-12-02",
        "2021-03-31",
        _params(equity_share=1.0),
        targets_by_day=_targets(states),
    )
    first_2021 = CAL[CAL.year == 2021][0]
    assert first_2021 in res.rebalance_days
    assert first_2021 not in res.switch_days
    assert res.bucket_weights.loc[first_2021, BUCKET_CORE] > 0.999


def test_targets_and_tech_share_are_mutually_exclusive() -> None:
    px = _flat_panel()
    cash = pd.Series(1.0, index=CAL)
    with pytest.raises(ValueError):
        simulate_static(
            px,
            px,
            cash,
            "2019-12-02",
            "2021-03-31",
            _params(),
            tech_share_by_day=pd.Series(1.0, index=CAL),
            targets_by_day=_targets(["GOOD"] * len(CAL)),
        )
