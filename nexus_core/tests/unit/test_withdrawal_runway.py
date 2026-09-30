import math

import pytest

from database.user_settings import (
    _normalize_anchor_month,
    _normalize_withdrawal_months,
)
from market_analysis import withdrawal_runway as wr
from market_analysis.withdrawal_runway import StressPath


def _flat(ret: float, days: int = 2600, cpi: float = 1.0) -> StressPath:
    return StressPath(tuple([ret] * days), tuple([cpi] * days))


def test_adjust_withdrawal_scales_with_cpi_and_falls_back() -> None:
    assert wr.adjust_withdrawal(10_000, 300.0, 330.0) == pytest.approx(11_000)
    assert wr.adjust_withdrawal(10_000, 300.0, None) == 10_000
    assert wr.adjust_withdrawal(10_000, 0.0, 330.0) == 10_000
    assert wr.adjust_withdrawal(-5, 300.0, 330.0) == 0.0


def test_zero_return_runway_and_boxx_payments() -> None:
    assert wr.zero_return_runway_years(100_000, 10_000) == pytest.approx(5.0)
    assert math.isinf(wr.zero_return_runway_years(100_000, 0))
    assert wr.boxx_payments(25_000, 10_000) == 2
    assert wr.boxx_payments(-1, 10_000) == 0
    assert wr.boxx_payments(5_000, 0) == 0


def test_zero_return_runway_follows_withdrawal_months() -> None:
    assert wr.zero_return_runway_years(100_000, 10_000, (1,)) == pytest.approx(10.0)
    assert wr.zero_return_runway_years(100_000, 10_000, (1, 4, 7, 10)) == pytest.approx(
        2.5
    )
    with pytest.raises(ValueError):
        wr.zero_return_runway_years(100_000, 10_000, ())
    with pytest.raises(ValueError):
        wr.zero_return_runway_years(100_000, 10_000, (0, 7))


def test_withdrawal_gaps_days() -> None:
    m = wr.TRADING_DAYS_PER_MONTH
    assert wr.withdrawal_gaps_days((1, 7)) == [6 * m, 6 * m]
    assert wr.withdrawal_gaps_days((1,)) == [12 * m]
    assert wr.withdrawal_gaps_days((7, 1, 3), first_month=3) == [4 * m, 6 * m, 2 * m]
    with pytest.raises(ValueError):
        wr.withdrawal_gaps_days((1, 7), first_month=3)


def test_clamp_beta() -> None:
    assert wr.clamp_beta(None) == wr.STRESS_BETA_FALLBACK
    assert wr.clamp_beta(float("nan")) == wr.STRESS_BETA_FALLBACK
    assert wr.clamp_beta(9.0) == 2.0
    assert wr.clamp_beta(-1.0) == 0.5
    assert wr.clamp_beta(1.1) == 1.1


def test_replay_flat_path_matches_zero_return_runway() -> None:
    # 零報酬、CPI 不變：10 次提領領完 10 萬，耗盡發生在第 10 次提領日
    first = 63
    years = wr.replay_years(100_000, 10_000, _flat(0.0), 1.0, days_to_first=first)
    expected_day = first + 9 * 6 * wr.TRADING_DAYS_PER_MONTH
    assert years == pytest.approx(expected_day / wr.TRADING_DAYS_PER_YEAR)


def test_replay_uses_configured_months() -> None:
    first = 63
    m = wr.TRADING_DAYS_PER_MONTH
    yearly = wr.replay_years(
        100_000, 10_000, _flat(0.0), 1.0, days_to_first=first, months=(1,)
    )
    assert yearly == pytest.approx((first + 9 * 12 * m) / wr.TRADING_DAYS_PER_YEAR)
    quarterly = wr.replay_years(
        100_000, 10_000, _flat(0.0), 1.0, days_to_first=first, months=(1, 4, 7, 10)
    )
    assert quarterly == pytest.approx((first + 9 * 3 * m) / wr.TRADING_DAYS_PER_YEAR)
    # 不等距月份：第一次在 3 月，之後 3→7（4 個月）、7→1（6 個月）、1→3（2 個月）
    uneven = wr.replay_years(
        30_000,
        10_000,
        _flat(0.0),
        1.0,
        days_to_first=1,
        months=(1, 3, 7),
        first_month=3,
    )
    assert uneven == pytest.approx((1 + 4 * m + 6 * m) / wr.TRADING_DAYS_PER_YEAR)


def test_replay_applies_day_k_return_on_day_k() -> None:
    """k = 0 為高點佔位；第 1 天必須套用 returns[1]，不是 returns[0]。"""
    rets = (0.0, -1.0) + (0.0,) * 10
    path = StressPath(rets, (1.0,) * len(rets))
    assert wr.replay_years(100_000, 1.0, path, 1.0, days_to_first=5) == pytest.approx(
        1 / wr.TRADING_DAYS_PER_YEAR
    )


def test_replay_capped_when_no_withdrawal_or_survives() -> None:
    assert wr.replay_years(100_000, 0, _flat(0.0), 1.0) == wr.STRESS_HORIZON_YEARS
    assert wr.replay_years(100_000, 10, _flat(0.001), 1.0) == wr.STRESS_HORIZON_YEARS
    assert wr.replay_years(0, 10_000, _flat(0.0), 1.0) == 0.0


def test_replay_cpi_growth_shortens_runway() -> None:
    flat = wr.replay_years(100_000, 10_000, _flat(0.0), 1.0, 1)
    inflated = wr.replay_years(100_000, 10_000, _flat(0.0, cpi=1.5), 1.0, 1)
    assert inflated < flat


def test_stress_runway_takes_worse_of_two_paths() -> None:
    paths = {wr.STRESS_PATH_GFC: _flat(0.0), wr.STRESS_PATH_DOTCOM: _flat(-0.0005)}
    res = wr.stress_runway(100_000, 0.0, 10_000, 1.0, 1, paths)
    assert res.stress_years == min(res.gfc_years, res.dotcom_years)
    assert res.dotcom_years < res.gfc_years


def test_stress_runway_boxx_protects_dotcom_path() -> None:
    paths = {wr.STRESS_PATH_GFC: _flat(-0.001), wr.STRESS_PATH_DOTCOM: _flat(-0.001)}
    no_boxx = wr.stress_runway(100_000, 0.0, 10_000, 1.0, 1, paths)
    with_boxx = wr.stress_runway(100_000, 50_000.0, 10_000, 1.0, 1, paths)
    assert with_boxx.dotcom_years > no_boxx.dotcom_years


def test_static_stress_paths_are_well_formed() -> None:
    paths = wr.load_stress_paths()
    assert set(paths) == {wr.STRESS_PATH_GFC, wr.STRESS_PATH_DOTCOM}
    for p in paths.values():
        assert len(p.returns) == len(p.cpi_growth) >= 2520
        assert p.cpi_growth[0] == 1.0 and p.cpi_growth[-1] > 1.0

    # 2008 自高點下跌、2000 更深
    def worst_dd(rets: tuple[float, ...]) -> float:
        nav = peak = 1.0
        dd = 0.0
        for r in rets:
            nav *= 1 + r
            peak = max(peak, nav)
            dd = max(dd, 1 - nav / peak)
        return dd

    assert 0.5 < worst_dd(paths["GFC"].returns) < 0.6
    assert 0.75 < worst_dd(paths["DOTCOM"].returns) < 0.85


def test_documented_stress_example_100k() -> None:
    """規格 §1.3 的 2026-09-30 試算：10 萬、每次 1 萬、無 BOXX。"""
    res = wr.stress_runway(100_000, 0.0, 10_000, 1.0, days_to_first=63)
    assert 3.3 <= res.gfc_years <= 4.3
    assert 2.0 <= res.dotcom_years <= 2.8
    assert res.stress_years == res.dotcom_years
    assert not res.capped


def test_plan_withdrawal_uses_boxx_first() -> None:
    plan = wr.plan_withdrawal(10_000, 12_000, {"NVDA": 50_000})
    assert plan.from_boxx == 10_000 and plan.sells == {} and plan.shortfall == 0.0


def test_plan_withdrawal_sells_most_overweight_first() -> None:
    holdings = {"NVDA": 60_000.0, "META": 20_000.0, "GOOGL": 20_000.0}
    plan = wr.plan_withdrawal(10_000, 0.0, holdings)
    assert plan.from_boxx == 0.0
    assert set(plan.sells) == {"NVDA"}
    assert plan.sells["NVDA"] == pytest.approx(10_000)


def test_plan_withdrawal_spreads_when_overweight_is_insufficient() -> None:
    holdings = {"A": 51_000.0, "B": 49_000.0}
    plan = wr.plan_withdrawal(20_000, 5_000, holdings)
    assert plan.from_boxx == 5_000
    assert sum(plan.sells.values()) == pytest.approx(15_000)
    assert all(plan.sells[s] <= holdings[s] for s in plan.sells)
    assert plan.sells["A"] > plan.sells["B"]


def test_plan_withdrawal_respects_target_weights_and_shortfall() -> None:
    plan = wr.plan_withdrawal(
        10_000, 0.0, {"A": 50_000.0, "B": 50_000.0}, {"A": 0.25, "B": 0.75}
    )
    assert set(plan.sells) == {"A"}
    short = wr.plan_withdrawal(50_000, 0.0, {"A": 10_000.0, "SHORT": -5_000.0})
    assert short.sells == {"A": pytest.approx(10_000)}
    assert short.shortfall == pytest.approx(40_000)
    empty = wr.plan_withdrawal(10_000, 2_000, {})
    assert empty.from_boxx == 2_000 and empty.shortfall == pytest.approx(8_000)


def test_evaluate_tiers_fires_once_and_rearms_with_buffer() -> None:
    armed = list(wr.RUNWAY_WARN_TIERS_YEARS)
    fired, armed = wr.evaluate_tiers(2.3, armed)
    assert fired == [3] and armed == [2, 1]
    fired, armed = wr.evaluate_tiers(2.4, armed)  # 仍低於 3，不重複
    assert fired == [] and armed == [2, 1]
    fired, armed = wr.evaluate_tiers(3.2, armed)  # 未達 3.5 → 不重新武裝
    assert fired == [] and armed == [2, 1]
    fired, armed = wr.evaluate_tiers(3.6, armed)
    assert fired == [] and armed == [3, 2, 1]


def test_evaluate_tiers_multiple_crossed_at_once() -> None:
    fired, armed = wr.evaluate_tiers(0.8, [3, 2, 1])
    assert fired == [1, 2, 3] and armed == []


def test_settings_normalizers() -> None:
    assert _normalize_anchor_month("2026-9") == "2026-09"
    assert _normalize_anchor_month("2026-13") is None
    assert _normalize_anchor_month("26-09") is None
    assert _normalize_withdrawal_months("7, 1,7") == "1,7"
    assert _normalize_withdrawal_months("") is None
    assert _normalize_withdrawal_months("0,7") is None
    assert _normalize_withdrawal_months("a") is None


def test_v085_migration_contract_and_columns(db_conn: object) -> None:
    """缺 version / description / sql 任一者的遷移會被無聲跳過。"""
    from database.core import get_migrations
    from database.migrations import v085_add_withdrawal_settings as m

    assert m.version == 85 and m.description and m.sql
    assert any(x["version"] == 85 for x in get_migrations())
    cols = {r[1] for r in db_conn.execute("PRAGMA table_info(user_settings)")}  # type: ignore[attr-defined]
    assert {
        "withdrawal_amount",
        "withdrawal_anchor_month",
        "withdrawal_months",
        "withdrawal_target_weights",
    } <= cols


def test_withdrawal_settings_roundtrip_and_defaults(db_conn: object) -> None:
    from database.user_settings import get_full_user_context, upsert_user_config

    uid = 990085
    upsert_user_config(uid, capital=100_000.0)
    ctx = get_full_user_context(uid)
    assert ctx.withdrawal_amount == 0.0  # 預設未啟用
    assert ctx.withdrawal_months == "1,7"
    assert ctx.withdrawal_anchor_month is None and ctx.withdrawal_target_weights is None

    upsert_user_config(
        uid,
        withdrawal_amount=10_000,
        withdrawal_anchor_month="2026-9",
        withdrawal_months="7,1",
        withdrawal_target_weights='{"NVDA": 0.5, "META": 0.5}',
    )
    ctx = get_full_user_context(uid)
    assert ctx.withdrawal_amount == 10_000.0
    assert ctx.withdrawal_anchor_month == "2026-09"
    assert ctx.withdrawal_months == "1,7"
    assert ctx.withdrawal_target_weights == '{"NVDA": 0.5, "META": 0.5}'

    # 非法值不覆寫既有設定，負數提領額夾為 0
    upsert_user_config(uid, withdrawal_anchor_month="bad", withdrawal_months="13")
    upsert_user_config(uid, withdrawal_amount=-5)
    ctx = get_full_user_context(uid)
    assert ctx.withdrawal_anchor_month == "2026-09" and ctx.withdrawal_months == "1,7"
    assert ctx.withdrawal_amount == 0.0
