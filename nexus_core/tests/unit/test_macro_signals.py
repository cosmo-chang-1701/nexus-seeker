"""market_analysis/macro_signals.py：可用日（前視防護）、警訊門檻、三態優先序與確認。"""

from datetime import date, timedelta

from market_analysis.macro_signals import (
    CONFIRM_DAYS,
    STATE_CAUTION,
    STATE_GOOD,
    STATE_WORST,
    Indicator,
    IndicatorReading,
    Observation,
    available_date_for,
    change_over,
    claims_surge,
    classify_raw,
    confirm_state,
    credit_spread_widen,
    level_at_least,
    tech_relative_weak,
    usable,
    vix_term_inversion,
)


def _daily(values: list[float], start: date = date(2025, 1, 1)) -> list[Observation]:
    return [
        Observation(start + timedelta(days=i), v, start + timedelta(days=i + 1))
        for i, v in enumerate(values)
    ]


def _reading(ind: Indicator, flag: bool | None) -> IndicatorReading:
    return IndicatorReading(ind, 0.0, flag, None, None)


# ---------------------------------------------------------------------------
# 可用日：判定只能用當天已公布的值
# ---------------------------------------------------------------------------


def test_daily_series_available_next_weekday() -> None:
    assert available_date_for("daily", date(2026, 9, 25)) == date(
        2026, 9, 28
    )  # 週五 → 週一
    assert available_date_for("daily", date(2026, 9, 28)) == date(2026, 9, 29)


def test_weekly_series_release_lag() -> None:
    # STLFSI4：週五觀測，隔週四公布
    assert available_date_for("weekly_stlfsi", date(2026, 9, 18)) == date(2026, 9, 24)
    # ICSA：週六觀測（週結束），隔週四公布
    assert available_date_for("weekly_claims", date(2026, 9, 19)) == date(2026, 9, 24)


def test_sahm_available_on_tenth_of_next_month() -> None:
    assert available_date_for("monthly_sahm", date(2026, 8, 1)) == date(2026, 9, 10)
    # 2026-10-10 是週六 → 順延到週一 10-12
    assert available_date_for("monthly_sahm", date(2026, 9, 1)) == date(2026, 10, 12)
    assert available_date_for("monthly_sahm", date(2026, 12, 1)) == date(2027, 1, 11)


def test_usable_excludes_unpublished_observations() -> None:
    obs = [
        Observation(date(2026, 8, 1), 0.3, date(2026, 9, 10)),
        Observation(date(2026, 9, 1), 0.6, date(2026, 10, 12)),
    ]
    got = usable(obs, date(2026, 10, 1))
    assert [o.value for o in got] == [0.3]
    # 公布後才可用
    assert [o.value for o in usable(obs, date(2026, 10, 12))] == [0.3, 0.6]


def test_level_indicator_uses_only_published_value() -> None:
    """9 月 Sahm 值已越過門檻，但 10-12 之前不得亮起。"""
    obs = [
        Observation(date(2026, 8, 1), 0.3, date(2026, 9, 10)),
        Observation(date(2026, 9, 1), 0.6, date(2026, 10, 12)),
    ]
    before = level_at_least(Indicator.SAHM_RULE, usable(obs, date(2026, 10, 9)), 0.5)
    after = level_at_least(Indicator.SAHM_RULE, usable(obs, date(2026, 10, 12)), 0.5)
    assert before.flag is False and before.as_of_date == date(2026, 8, 1)
    assert after.flag is True and after.available_date == date(2026, 10, 12)


# ---------------------------------------------------------------------------
# 警訊門檻
# ---------------------------------------------------------------------------


def test_change_over_threshold_and_insufficient_data() -> None:
    obs = _daily([1.0] * 126 + [2.0])  # 126 筆前為 1.0，最新 2.0 → +1.0 pp
    r = change_over(Indicator.REAL_YIELD_JUMP, obs, 126, 1.0)
    assert r.flag is True and r.value == 1.0
    r2 = change_over(Indicator.REAL_YIELD_JUMP, _daily([1.0] * 126 + [1.99]), 126, 1.0)
    assert r2.flag is False
    short = change_over(Indicator.TWO_YEAR_JUMP, _daily([1.0] * 126), 126, 1.0)
    assert short.flag is None and short.value is None


def test_credit_spread_widen_ratio() -> None:
    obs = _daily([2.0] * 125 + [2.6])  # 均值 (2*125+2.6)/126 ≈ 2.0048 → 比值 ≈ 1.297
    assert credit_spread_widen(obs).flag is True
    assert credit_spread_widen(_daily([2.0] * 126)).flag is False
    assert credit_spread_widen(_daily([2.0] * 10)).flag is None


def test_claims_surge_vs_52_week_low() -> None:
    base = [200_000.0] * 60
    rising = base[:-4] + [250_000.0] * 4  # 4 週平均 250k，較低點 200k 上升 25%
    weekly = [
        Observation(
            date(2025, 1, 4) + timedelta(weeks=i),
            v,
            date(2025, 1, 9) + timedelta(weeks=i),
        )
        for i, v in enumerate(rising)
    ]
    r = claims_surge(weekly)
    assert r.flag is True and abs((r.value or 0) - 0.25) < 1e-9
    flat = [
        Observation(date(2025, 1, 4) + timedelta(weeks=i), 200_000.0, date(2025, 1, 9))
        for i in range(60)
    ]
    assert claims_surge(flat).flag is False
    assert claims_surge(flat[:10]).flag is None


def test_vix_term_inversion() -> None:
    d = date(2026, 3, 1)
    assert vix_term_inversion(30.0, 25.0, d).flag is True
    assert vix_term_inversion(15.0, 18.0, d).flag is False
    assert vix_term_inversion(None, 18.0, d).flag is None


def test_tech_relative_weak_equal_weight() -> None:
    d = date(2026, 3, 1)
    voo = [100.0] * 63 + [110.0]  # VOO +10%
    tech = {"A": [100.0] * 63 + [104.0], "B": [100.0] * 63 + [106.0]}  # 等權 +5%
    r = tech_relative_weak(tech, voo, d)
    assert r.flag is True and abs((r.value or 0) + 5.0) < 1e-9
    strong = {"A": [100.0] * 63 + [120.0]}
    assert tech_relative_weak(strong, voo, d).flag is False
    assert tech_relative_weak({}, voo, d).flag is None


# ---------------------------------------------------------------------------
# 三態優先序與確認
# ---------------------------------------------------------------------------


def test_classify_priority_tier2_over_tier1() -> None:
    both = [
        _reading(Indicator.REAL_YIELD_JUMP, True),
        _reading(Indicator.CLAIMS_SURGE, True),
    ]
    assert classify_raw(both) == STATE_WORST
    assert classify_raw([_reading(Indicator.TECH_RELATIVE_WEAK, True)]) == STATE_CAUTION
    assert classify_raw([_reading(Indicator.FIN_STRESS, False)]) == STATE_GOOD


def test_control_indicators_and_missing_do_not_count() -> None:
    readings = [
        _reading(Indicator.SAHM_RULE, True),
        _reading(Indicator.CREDIT_SPREAD_WIDEN, True),
        _reading(Indicator.FED_HIKE_CYCLE, True),
        _reading(Indicator.VIX_TERM_INVERSION, None),
    ]
    assert classify_raw(readings) == STATE_GOOD


def test_confirm_requires_consecutive_days() -> None:
    assert CONFIRM_DAYS == 5
    # 暖機期：不足 5 天、沒有前一個確認狀態
    assert confirm_state([STATE_GOOD] * 3, None) is None
    assert confirm_state([STATE_GOOD] * 5, None) == STATE_GOOD
    # 4 天 WORST 不足以切換，維持前一個確認狀態
    assert confirm_state([STATE_GOOD] + [STATE_WORST] * 4, STATE_GOOD) == STATE_GOOD
    assert confirm_state([STATE_WORST] * 5, STATE_GOOD) == STATE_WORST
    # 中間夾一天不同就重來
    mixed = [STATE_CAUTION, STATE_CAUTION, STATE_GOOD, STATE_CAUTION, STATE_CAUTION]
    assert confirm_state(mixed, STATE_GOOD) == STATE_GOOD
