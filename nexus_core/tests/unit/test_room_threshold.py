"""動態自適應波動率空間門檻 (market_analysis/room_threshold.py) 單元測試。

涵蓋三組公式：
  A. compute_dynamic_room_threshold  —— max(2.2×Risk, 1.5×ATR₁D, 3.5%)
  B. evaluate_wall_buffer            —— 依 Regime 分流的緩衝雙邊界三態
  C. evaluate_next_strike_space      —— 破位追空次級節點空間

以及共用的 resolve_atr_15m 量綱折算階梯。
"""

import math

import pytest

from market_analysis.room_threshold import (
    _ATR_14_PLACEHOLDER,
    BufferProfile,
    _BARS_PER_SESSION,
    _BUFFER_MAX_STOP_DISTANCE_PCT,
    _LEGACY_BUFFER_MAX_PCT,
    _ROOM_ABSOLUTE_FLOOR_PCT,
    compute_dynamic_room_threshold,
    evaluate_next_strike_space,
    evaluate_wall_buffer,
    resolve_atr_15m,
)


# --------------------------------------------------------------------------
# 公式 A：三項各自 binding
# --------------------------------------------------------------------------
def test_risk_term_binds_when_downside_risk_dominates() -> None:
    """Spot 100 / PutWall 95 / ATR₁₅ₘ 1.0 → Stop 94.5，Risk 5.5%，
    2.2×5.5% = 12.1% 壓過 1.5×ATR₁D(1.5%) 與 3.5% 底線。

    停損墊片是 0.5×ATR₁₅ₘ，與 anti_washout.py 軌道一實際執行的停損一致。
    """
    room = compute_dynamic_room_threshold(100.0, 95.0, 1.0, 1.0, direction="LONG")
    assert room.risk_actual_pct == pytest.approx(0.055)
    assert room.threshold_pct == pytest.approx(2.2 * 0.055)
    assert room.binding_term == "RISK"
    assert room.is_degraded is False
    assert room.degrade_reason is None


def test_atr_1d_term_binds_when_wall_is_hugged() -> None:
    """現價緊貼 PutWall 且 ATR₁₅ₘ 極小 → Risk 幾近於零，改由單日波幅項決定。"""
    room = compute_dynamic_room_threshold(100.0, 99.9, 0.01, 8.0, direction="LONG")
    # Risk = (100 - (99.9 - 0.005)) / 100 = 0.105% → 2.2× = 0.231%
    assert room.threshold_pct == pytest.approx(1.5 * 0.08)
    assert room.binding_term == "ATR_1D"
    assert room.is_degraded is False


def test_floor_binds_for_ultra_low_volatility_symbol() -> None:
    """低波標的：兩項推導值皆低於 3.5%，由絕對底線接管。"""
    room = compute_dynamic_room_threshold(100.0, 99.5, 0.05, 1.0, direction="LONG")
    assert room.threshold_pct == pytest.approx(_ROOM_ABSOLUTE_FLOOR_PCT)
    assert room.binding_term == "FLOOR"
    assert room.is_degraded is False


def test_short_direction_mirrors_long() -> None:
    """做空：Stop = CallWall + 1.5×ATR₁₅ₘ，Risk 為上行風險，量值與多頭對稱。"""
    long_room = compute_dynamic_room_threshold(100.0, 95.0, 1.0, 1.0, direction="LONG")
    short_room = compute_dynamic_room_threshold(
        100.0, 105.0, 1.0, 1.0, direction="SHORT"
    )
    assert short_room.risk_actual_pct == pytest.approx(long_room.risk_actual_pct)
    assert short_room.threshold_pct == pytest.approx(long_room.threshold_pct)
    assert short_room.binding_term == "RISK"


# --------------------------------------------------------------------------
# 公式 A：退化與夾值
# --------------------------------------------------------------------------
def test_stop_above_spot_degrades_to_spot_minus_two_atr() -> None:
    """PutWall 高於現價 (牆體拓撲逆轉) → 改用 現價 − 2.0×ATR₁₅ₘ 作為停損。

    沿用 portfolio_embeds.py 既有的「PutWall異常降級」慣例，不另立第二套邏輯。
    """
    room = compute_dynamic_room_threshold(100.0, 110.0, 1.0, 1.0, direction="LONG")
    # Stop = 110 - 0.5 = 109.5 >= 100 → fallback 100 - 2.0 = 98.0 → Risk 2%
    assert room.risk_actual_pct == pytest.approx(0.02)
    assert room.is_degraded is False  # 輸入皆齊全，只是牆體異常，非資料缺失


def test_short_stop_below_spot_degrades_symmetrically() -> None:
    room = compute_dynamic_room_threshold(100.0, 90.0, 1.0, 1.0, direction="SHORT")
    # Stop = 90 + 0.5 = 90.5 <= 100 → fallback 100 + 2.0 = 102.0 → Risk 2%
    assert room.risk_actual_pct == pytest.approx(0.02)


def test_risk_is_clamped_to_non_negative() -> None:
    """停損落在進場價的錯誤一側時數學上退化，Risk 夾為 0 而非反向壓低門檻。"""
    room = compute_dynamic_room_threshold(100.0, 100.0, 0.0001, 0.0, direction="LONG")
    assert room.risk_actual_pct is not None
    assert room.risk_actual_pct >= 0.0


# --------------------------------------------------------------------------
# 公式 A：四種降級路徑
# --------------------------------------------------------------------------
def test_missing_put_wall_drops_risk_term_and_flags_degraded() -> None:
    room = compute_dynamic_room_threshold(100.0, 0.0, 1.0, 8.0, direction="LONG")
    assert room.risk_actual_pct is None
    assert room.threshold_pct == pytest.approx(1.5 * 0.08)
    assert room.is_degraded is True
    assert room.degrade_reason is not None
    assert "PutWall" in room.degrade_reason


def test_missing_atr_15m_drops_risk_term() -> None:
    room = compute_dynamic_room_threshold(100.0, 95.0, 0.0, 8.0, direction="LONG")
    assert room.risk_actual_pct is None
    assert room.is_degraded is True
    assert room.degrade_reason is not None and "ATR₁₅ₘ" in room.degrade_reason


def test_missing_atr_1d_drops_volatility_term() -> None:
    room = compute_dynamic_room_threshold(100.0, 95.0, 1.0, 0.0, direction="LONG")
    assert room.atr_1d_pct is None
    assert room.binding_term == "RISK"
    assert room.is_degraded is True
    assert room.degrade_reason is not None and "ATR₁D" in room.degrade_reason


def test_all_inputs_missing_falls_back_to_absolute_floor() -> None:
    room = compute_dynamic_room_threshold(100.0, 0.0, 0.0, 0.0, direction="LONG")
    assert room.threshold_pct == pytest.approx(_ROOM_ABSOLUTE_FLOOR_PCT)
    assert room.binding_term == "FLOOR"
    assert room.is_degraded is True
    assert room.degrade_reason is not None
    assert "3.5%" in room.degrade_reason and "絕對底線" in room.degrade_reason


def test_invalid_spot_returns_floor_immediately() -> None:
    room = compute_dynamic_room_threshold(0.0, 95.0, 1.0, 1.0, direction="LONG")
    assert room.threshold_pct == pytest.approx(_ROOM_ABSOLUTE_FLOOR_PCT)
    assert room.is_degraded is True
    assert room.degrade_reason is not None and "現價" in room.degrade_reason


def test_short_direction_degrade_reason_names_call_wall() -> None:
    """做空缺牆時，缺失項名稱須是 CallWall 而非 PutWall。"""
    room = compute_dynamic_room_threshold(100.0, 0.0, 1.0, 1.0, direction="SHORT")
    assert room.degrade_reason is not None
    assert "CallWall" in room.degrade_reason


def test_nan_and_inf_inputs_are_treated_as_missing() -> None:
    room = compute_dynamic_room_threshold(
        100.0, float("nan"), float("inf"), 0.0, direction="LONG"
    )
    assert room.risk_actual_pct is None
    assert room.threshold_pct == pytest.approx(_ROOM_ABSOLUTE_FLOOR_PCT)
    assert room.is_degraded is True


# --------------------------------------------------------------------------
# 公式 B：緩衝雙邊界三態 × 三組 profile
# --------------------------------------------------------------------------
def test_buffer_too_tight_is_rejected() -> None:
    """停損距現價 1.0%，低於下界 2.5×ATR₁₅ₘ(=2.5%) → 易遭 Liquidity Sweep。

    ⚠️ 這正是新增下界閘門的行為變化：舊版 0 < d <= 5% 會放行此情境。
    """
    # 支撐牆 99.5 → 停損 = 99.5 − 0.5×1.0 = 99.0 → 停損距離 1.0%
    buf = evaluate_wall_buffer(100.0, 99.5, 1.0, 5.0, profile="RIGHT")
    assert buf.buffer_pct == pytest.approx(0.010)
    assert buf.min_pct == pytest.approx(0.025)
    assert buf.state == "TOO_TIGHT"
    assert buf.passed is False


def test_buffer_sweet_spot_passes() -> None:
    # 支撐牆 96.0 → 停損 95.5 → 停損距離 4.5%，落在 [2.5%, 8%]
    buf = evaluate_wall_buffer(100.0, 96.0, 1.0, 5.0, profile="RIGHT")
    assert buf.buffer_pct == pytest.approx(0.045)
    assert buf.state == "SWEET_SPOT"
    assert buf.passed is True
    assert buf.is_degraded is False


def test_buffer_too_wide_is_rejected_by_absolute_cap() -> None:
    """上界是**絕對** 8%，不隨 ATR 伸縮。

    早期版本用 1.8×ATR₁D，對低波標的會產生窄於一個履約價間距的可接受帶，
    等同抽籤；且與條件三的 2.2×Risk 重複定價同一風險。
    """
    buf = evaluate_wall_buffer(100.0, 91.0, 1.0, 5.0, profile="RIGHT")
    assert buf.buffer_pct == pytest.approx(0.095)
    assert buf.max_pct == pytest.approx(_BUFFER_MAX_STOP_DISTANCE_PCT)
    assert buf.state == "TOO_WIDE"
    assert buf.passed is False


def test_absolute_cap_does_not_shrink_for_low_volatility_symbols() -> None:
    """低波標的 (ATR₁D 0.8%) 的上界仍是 8%，不會縮成 1.44%。

    這是本次修正的核心：ATR 縮放的上界在低波標的上會窄到容不下一個履約價。
    """
    low_vol = evaluate_wall_buffer(100.0, 96.0, 0.157, 0.8, profile="RIGHT")
    high_vol = evaluate_wall_buffer(100.0, 96.0, 0.98, 5.0, profile="RIGHT")
    assert low_vol.max_pct == high_vol.max_pct == pytest.approx(0.08)
    assert low_vol.state == "SWEET_SPOT"  # 舊版 1.8×0.8% = 1.44% 會判 TOO_WIDE


def test_left_profile_allows_the_tight_put_wall_hug() -> None:
    """左側密著帶在 RIGHT 剖面會被判 TOO_TIGHT，在 LEFT 剖面須通過。

    這就是下界倍率必須依 Regime 分流的原因：沿用 2.5× 會讓左側六重鐵律
    永遠無法通過條件二。
    """
    right = evaluate_wall_buffer(100.0, 100.0, 1.0, 5.0, profile="RIGHT")
    left = evaluate_wall_buffer(100.0, 100.0, 1.0, 5.0, profile="LEFT")
    # 現價正好貼齊 Put Wall：停損 = 99.5 → 停損距離恆等於 0.5×ATR₁₅ₘ
    assert right.buffer_pct == left.buffer_pct == pytest.approx(0.005)
    assert right.min_pct == pytest.approx(0.025)
    assert left.min_pct == pytest.approx(0.005)
    assert right.state == "TOO_TIGHT"
    assert left.state == "SWEET_SPOT"


def test_left_lower_bound_is_structurally_tied_to_stop_multiplier() -> None:
    """左側下界必須 <= 停損墊片倍數，否則策略的設計中心點永遠無法通過。

    貼牆時停損距離 == _ROOM_STOP_ATR_15M_MULTIPLIER × ATR₁₅ₘ，所以
    _BUFFER_LOWER_MULTIPLIERS["LEFT"] 一旦大於它，left 側條件二即恆為 False。
    """
    from market_analysis.room_threshold import (
        _BUFFER_LOWER_MULTIPLIERS,
        _ROOM_STOP_ATR_15M_MULTIPLIER,
    )

    assert _BUFFER_LOWER_MULTIPLIERS["LEFT"] <= _ROOM_STOP_ATR_15M_MULTIPLIER


def test_all_profiles_measure_stop_distance_not_wall_distance() -> None:
    """三種剖面統一量停損距離——語意一致是本次重構的重點之一。"""
    # 牆距 3.0%，停損墊片 0.5×1.0/100 = 0.5% → 停損距離必為 3.5%
    long_profiles: tuple[BufferProfile, ...] = ("RIGHT", "LEFT")
    for profile in long_profiles:
        buf = evaluate_wall_buffer(100.0, 97.0, 1.0, 5.0, profile=profile)
        assert buf.buffer_pct == pytest.approx(0.035), profile
    short = evaluate_wall_buffer(100.0, 103.0, 1.0, 5.0, profile="SHORT")
    assert short.buffer_pct == pytest.approx(0.035)


def test_left_profile_rejects_deep_puncture_with_stop_underfoot() -> None:
    """LEFT 下界仍會咬合：現價已深度穿刺 Put Wall、停損就在腳邊時應判過窄。"""
    # Put Wall 100，現價 99.6；停損 = 100 − 0.5 = 99.5，距現價僅 0.1%
    buf = evaluate_wall_buffer(99.6, 100.0, 1.0, 5.0, profile="LEFT")
    assert buf.min_pct is not None
    assert buf.buffer_pct < buf.min_pct
    assert buf.state == "TOO_TIGHT"


def test_short_profile_measures_wall_above_spot() -> None:
    """做空剖面的牆體在現價上方，停損 = Wall + 0.5×ATR₁₅ₘ。"""
    buf = evaluate_wall_buffer(100.0, 104.0, 1.0, 5.0, profile="SHORT")
    assert buf.buffer_pct == pytest.approx(0.045)
    assert buf.state == "SWEET_SPOT"


def test_short_profile_wall_below_spot_degrades_to_fallback_stop() -> None:
    """阻力牆落在現價下方 (資料異常) → 走 現價 + 2.0×ATR₁₅ₘ 替代墊片。"""
    buf = evaluate_wall_buffer(100.0, 96.0, 1.0, 5.0, profile="SHORT")
    # Stop = 96 + 0.5 = 96.5 <= 100 → fallback 100 + 2.0 = 102.0 → 2.0%
    assert buf.buffer_pct == pytest.approx(0.020)


def test_buffer_degrades_to_legacy_single_bound_without_atr_15m() -> None:
    """ATR₁₅ₘ 缺失 → 無從推導停損，退回舊版 0 < 牆距 <= 5% 單邊判定。"""
    inside = evaluate_wall_buffer(100.0, 97.0, 0.0, 5.0, profile="RIGHT")
    assert inside.buffer_pct == pytest.approx(0.03)  # 量牆距
    assert inside.state == "SWEET_SPOT"
    assert inside.is_degraded is True
    assert inside.degrade_reason is not None
    assert f"{_LEGACY_BUFFER_MAX_PCT:.0%}" in inside.degrade_reason

    outside = evaluate_wall_buffer(100.0, 90.0, 0.0, 5.0, profile="RIGHT")
    assert outside.state == "TOO_WIDE"

    breached = evaluate_wall_buffer(100.0, 101.0, 0.0, 5.0, profile="RIGHT")
    assert breached.state == "TOO_TIGHT"


def test_buffer_ignores_atr_1d(  # noqa: D103
) -> None:
    """上界改絕對值後，ATR₁D 不再參與判定；傳 0.0 不得改變結果。"""
    with_atr = evaluate_wall_buffer(100.0, 96.0, 1.0, 5.0, profile="RIGHT")
    without = evaluate_wall_buffer(100.0, 96.0, 1.0, 0.0, profile="RIGHT")
    assert with_atr == without


def test_buffer_missing_wall_is_degraded_and_rejected() -> None:
    buf = evaluate_wall_buffer(100.0, 0.0, 1.0, 5.0, profile="RIGHT")
    assert buf.state == "TOO_TIGHT"
    assert buf.passed is False
    assert buf.is_degraded is True


# --------------------------------------------------------------------------
# 公式 C：破位追空次級節點空間
# --------------------------------------------------------------------------
def test_next_strike_space_passes_when_gap_exceeds_two_daily_atr() -> None:
    passed, space, required = evaluate_next_strike_space(100.0, 88.0, 5.0)
    assert space == pytest.approx(0.12)
    assert required == pytest.approx(0.10)
    assert passed is True


def test_next_strike_space_fails_when_gap_too_small() -> None:
    passed, space, required = evaluate_next_strike_space(100.0, 95.0, 5.0)
    assert space == pytest.approx(0.05)
    assert required == pytest.approx(0.10)
    assert passed is False


def test_next_strike_space_fails_closed_on_missing_data() -> None:
    """追空是進攻動作，無法確認空間一律不進場 (fail-closed)。"""
    assert evaluate_next_strike_space(100.0, 0.0, 5.0) == (False, 0.0, None)
    assert evaluate_next_strike_space(100.0, 88.0, 0.0) == (False, 0.0, None)
    assert evaluate_next_strike_space(0.0, 88.0, 5.0) == (False, 0.0, None)


# --------------------------------------------------------------------------
# resolve_atr_15m 量綱折算階梯
# --------------------------------------------------------------------------
def test_resolve_atr_15m_prefers_real_value() -> None:
    assert resolve_atr_15m(2.0, 10.0) == pytest.approx(2.0)


def test_resolve_atr_15m_scales_daily_by_sqrt_26() -> None:
    assert resolve_atr_15m(0.0, 10.2) == pytest.approx(
        10.2 / math.sqrt(_BARS_PER_SESSION)
    )


def test_resolve_atr_15m_rejects_the_0_01_placeholder() -> None:
    """EnhancedWatchlistMetrics.atr_14 因 gt=0.0 約束而無法寫 0，取不到日線 ATR
    時寫入的是 0.01 佔位值。直接採用會得到 $0.015 的假緩衝，必須視為缺失。"""
    assert resolve_atr_15m(0.0, _ATR_14_PLACEHOLDER) == 0.0


def test_resolve_atr_15m_returns_zero_when_both_missing() -> None:
    assert resolve_atr_15m(0.0, 0.0) == 0.0
