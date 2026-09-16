"""SHORT_ENTRY 價位與倉位計算 (dynamic_rollover/short_entry_sizing.py) 單元測試。"""

import pytest

from market_analysis.dynamic_rollover.short_entry_sizing import (
    ShortEntryLevels,
    build_short_entry_levels,
    compute_short_entry_sizing,
)
from tests.unit.short_entry_helpers import make_short_entry_evaluation


def _levels(**overrides: object) -> ShortEntryLevels:
    base: dict = {
        "sub_mode": "區間內做空",
        "entry_price": 100.0,
        "stop_price": 105.0,
        "stop_price_structural": 104.5,
        "stop_price_exit_engine": 105.0,
        "target_price": 90.0,
        "reward_risk_ratio": 2.0,
        "invalidation_note": None,
    }
    base.update(overrides)
    return ShortEntryLevels(**base)


# ---------------------------------------------------------------- 價位
class TestLevels:
    def test_range_mode_uses_put_wall_and_farther_stop(self) -> None:
        levels = build_short_entry_levels(make_short_entry_evaluation())
        assert levels is not None
        assert levels.entry_price == 100.0
        assert levels.stop_price_structural == 104.5  # 頂牆 104 + 0.5×ATR
        assert levels.stop_price_exit_engine == 105.5  # Call Wall 105 + 0.5×ATR
        assert levels.stop_price == 105.5  # 取較遠者
        assert levels.target_price == 90.0
        assert levels.reward_risk_ratio == pytest.approx(10.0 / 5.5, abs=0.01)
        assert levels.invalidation_note is None

    def test_breakdown_chase_targets_next_node_with_invalidation_note(self) -> None:
        ev = make_short_entry_evaluation(
            sub_mode="破位追空",
            spot=92.0,
            put_wall=95.0,
            resistance_wall=97.0,
            call_wall=97.0,
            next_negative_node=80.0,
        )
        levels = build_short_entry_levels(ev)
        assert levels is not None
        assert levels.target_price == 80.0
        assert levels.stop_price == 97.5
        assert levels.invalidation_note is not None
        assert "收復 Put Wall $95.00" in levels.invalidation_note

    @pytest.mark.parametrize(
        "overrides",
        [
            {"put_wall": 0.0},  # 無目標
            {"put_wall": 101.0},  # 目標在進場之上
            {"atr_15m": 0.0},  # 無 ATR 無法推導停損
            {"sub_mode": "破位追空", "next_negative_node": 0.0},
            {"spot": 0.0},
        ],
    )
    def test_invalid_levels_fail_closed(self, overrides: dict) -> None:
        assert (
            build_short_entry_levels(make_short_entry_evaluation(**overrides)) is None
        )

    def test_stop_below_entry_is_excluded(self) -> None:
        """出場引擎錨點退回現價 (Call Wall 缺失) 時該停損不具參考意義，
        只剩結構停損可用。"""
        levels = build_short_entry_levels(
            make_short_entry_evaluation(call_wall=0.0, gamma_flip=0.0)
        )
        assert levels is not None
        assert levels.stop_price == 104.5


# ---------------------------------------------------------------- 倉位
class TestSizing:
    def test_risk_pct_binding_formula(self) -> None:
        """資本 100k × 0.5% × VIX Ready 1.0 = $500；停損距離 $5 → 100 股。"""
        sizing = compute_short_entry_sizing(
            _levels(),
            capital=100_000.0,
            risk_limit_pct=15.0,
            vix_spot=20.0,
            rsi_15m=40.0,
        )
        assert sizing.binding_constraint == "RISK_PCT"
        assert sizing.risk_budget_usd == pytest.approx(500.0)
        assert sizing.share_qty == 100
        assert sizing.notional_usd == pytest.approx(10_000.0)
        assert sizing.short_vix_multiplier == 1.0

    def test_vix_multiplier_scales_budget(self) -> None:
        sizing = compute_short_entry_sizing(
            _levels(), 100_000.0, 15.0, vix_spot=31.0, rsi_15m=40.0
        )
        assert sizing.short_vix_multiplier == 0.5
        assert sizing.share_qty == 50

    def test_vix_extreme_yields_zero(self) -> None:
        sizing = compute_short_entry_sizing(_levels(), 100_000.0, 15.0, 36.0, 40.0)
        assert sizing.binding_constraint == "VIX_ZERO"
        assert sizing.share_qty == 0

    def test_unknown_vix_is_conservative(self) -> None:
        sizing = compute_short_entry_sizing(_levels(), 100_000.0, 15.0, None, 40.0)
        assert sizing.short_vix_multiplier == 0.5
        assert sizing.degrade_reason is not None
        assert sizing.vix_tier_name == "未知"

    def test_poor_reward_risk_has_no_edge(self) -> None:
        """R:R 1.0、做空先驗勝率 0.45 → 凱利 f = 0.45 − 0.55/1 < 0。"""
        sizing = compute_short_entry_sizing(
            _levels(reward_risk_ratio=1.0), 100_000.0, 15.0, 20.0, 40.0
        )
        assert sizing.binding_constraint == "NO_EDGE"
        assert sizing.share_qty == 0

    def test_kelly_binding_when_edge_is_thin(self) -> None:
        """R:R 1.25：f = 0.45 − 0.55/1.25 = 0.01，×0.5 = 0.005 → 恰與上限相同；
        R:R 1.23 則低於 0.5%，凱利成為約束。"""
        sizing = compute_short_entry_sizing(
            _levels(reward_risk_ratio=1.23), 100_000.0, 15.0, 20.0, 40.0
        )
        assert sizing.binding_constraint == "KELLY"
        assert 0 < sizing.risk_budget_usd < 500.0

    def test_exposure_cap_binding(self) -> None:
        """risk_limit 1% → 名目上限 $1,000 → 10 股，低於風險預算的 100 股。"""
        sizing = compute_short_entry_sizing(_levels(), 100_000.0, 1.0, 20.0, 40.0)
        assert sizing.binding_constraint == "EXPOSURE_CAP"
        assert sizing.share_qty == 10

    def test_reward_risk_above_prior_odds_is_not_trusted(self) -> None:
        """R:R 5.0 與 1.8 的凱利分數相同 (賠率取 min(R:R, 先驗 1.8))。"""
        a = compute_short_entry_sizing(
            _levels(reward_risk_ratio=5.0), 100_000.0, 15.0, 20.0, 60.0
        )
        b = compute_short_entry_sizing(
            _levels(reward_risk_ratio=1.8), 100_000.0, 15.0, 20.0, 60.0
        )
        assert a.kelly_fraction == b.kelly_fraction

    @pytest.mark.parametrize(
        "kwargs",
        [
            {"capital": 0.0},
            {"levels": _levels(stop_price=99.0)},
        ],
    )
    def test_invalid_input(self, kwargs: dict) -> None:
        params: dict = {
            "levels": _levels(),
            "capital": 100_000.0,
            "risk_limit_pct": 15.0,
            "vix_spot": 20.0,
            "rsi_15m": 40.0,
        }
        params.update(kwargs)
        sizing = compute_short_entry_sizing(**params)
        assert sizing.binding_constraint == "INVALID_INPUT"
        assert sizing.share_qty == 0
