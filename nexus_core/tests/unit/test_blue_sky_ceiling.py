"""晴空萬里天花板擴展 (market_analysis/room_threshold.py 公式 D) 單元測試。

涵蓋 ``resolve_effective_target()``：標的貼近／突破 60 日高點時，以 60 日高點
與 ATR 外推目標取代裸 Call Wall，解除創新高標的被結構性誤判為封頂的問題
（見 handoff.md §1.4、§3.3）。
"""

import pytest

from market_analysis.room_threshold import (
    _BLUE_SKY_ATR_MULTIPLIER,
    resolve_effective_target,
)


def test_far_below_60d_high_returns_bare_call_wall() -> None:
    """Spot 遠低於 60 日高點 → 回傳裸 Call Wall，現行行為不變（最高優先回歸測項）。"""
    result = resolve_effective_target(
        spot=100.0, call_wall=105.0, high_60d=150.0, atr_1d=2.0
    )
    assert result.target == pytest.approx(105.0)
    assert result.is_blue_sky is False
    assert result.is_degraded is False
    assert result.degrade_reason is None


def test_new_high_returns_atr_extrapolated_target() -> None:
    """Spot 創新高（突破 60 日高點）→ ATR 外推目標成為最大值。"""
    result = resolve_effective_target(
        spot=101.0, call_wall=101.5, high_60d=100.0, atr_1d=2.0
    )
    expected = 101.0 + _BLUE_SKY_ATR_MULTIPLIER * 2.0
    assert result.target == pytest.approx(expected)
    assert result.is_blue_sky is True
    assert result.is_degraded is False


def test_within_proximity_band_enables_extension() -> None:
    """Spot 距 60 日高點 1.5%（< 2% 門檻）→ 啟用晴空萬里擴展。"""
    high_60d = 100.0
    spot = 98.5  # 距高點 1.5%
    result = resolve_effective_target(
        spot=spot, call_wall=99.0, high_60d=high_60d, atr_1d=1.0
    )
    assert result.is_blue_sky is True
    expected = max(99.0, high_60d, spot + _BLUE_SKY_ATR_MULTIPLIER * 1.0)
    assert result.target == pytest.approx(expected)


def test_outside_proximity_band_disables_extension() -> None:
    """Spot 距 60 日高點 3%（> 2% 門檻）→ 不啟用擴展，回傳裸 Call Wall。"""
    result = resolve_effective_target(
        spot=97.0, call_wall=98.0, high_60d=100.0, atr_1d=1.0
    )
    assert result.is_blue_sky is False
    assert result.target == pytest.approx(98.0)


# --------------------------------------------------------------------------
# 三條降級路徑
# --------------------------------------------------------------------------
def test_degrades_when_high_60d_missing_but_triggers_fail_open() -> None:
    """60 日高點缺失 → 自 max() 剔除該項，且觸發判定 fail-open（無從驗證是否
    貼近前高時，寧可多算一次擴展，也不要誤判創新高標的為封頂）。"""
    result = resolve_effective_target(
        spot=100.0, call_wall=101.0, high_60d=0.0, atr_1d=2.0
    )
    assert result.is_blue_sky is True
    assert result.is_degraded is True
    assert result.degrade_reason is not None
    assert "60 日高點" in result.degrade_reason
    expected = max(101.0, 100.0 + _BLUE_SKY_ATR_MULTIPLIER * 2.0)
    assert result.target == pytest.approx(expected)


def test_degrades_when_atr_1d_missing() -> None:
    """ATR₁D 缺失，Spot 已貼近 60 日高點 → 自 max() 剔除該項，其餘照常。"""
    result = resolve_effective_target(
        spot=100.0, call_wall=95.0, high_60d=100.0, atr_1d=0.0
    )
    assert result.is_blue_sky is True
    assert result.is_degraded is True
    assert result.degrade_reason is not None
    assert "ATR" in result.degrade_reason
    assert result.target == pytest.approx(100.0)  # max(95.0, 100.0)


def test_degrades_to_bare_call_wall_when_both_missing() -> None:
    """60 日高點與 ATR₁D 皆缺失 → 退回裸 Call Wall（即現行行為），標記降級。"""
    result = resolve_effective_target(
        spot=100.0, call_wall=102.0, high_60d=0.0, atr_1d=0.0
    )
    assert result.target == pytest.approx(102.0)
    assert result.is_blue_sky is False
    assert result.is_degraded is True
    assert result.degrade_reason is not None
    assert "60 日高點" in result.degrade_reason and "ATR" in result.degrade_reason


def test_missing_spot_is_fail_safe() -> None:
    """現價缺失 → 無法解析，回傳 0.0 並標記降級（不拋例外）。"""
    result = resolve_effective_target(
        spot=0.0, call_wall=100.0, high_60d=100.0, atr_1d=1.0
    )
    assert result.target == pytest.approx(0.0)
    assert result.is_degraded is True


def test_call_wall_missing_still_yields_blue_sky_target() -> None:
    """Call Wall 缺失但已貼近 60 日高點 → 仍以 60 日高點／ATR 外推算出天花板。"""
    result = resolve_effective_target(
        spot=100.0, call_wall=0.0, high_60d=100.0, atr_1d=1.0
    )
    assert result.is_blue_sky is True
    expected = max(100.0, 100.0 + _BLUE_SKY_ATR_MULTIPLIER * 1.0)
    assert result.target == pytest.approx(expected)


# --------------------------------------------------------------------------
# 一致性：純函式，相同輸入必得相同輸出——三個消費端 (regime_classifier.py /
# opportunity_cost.py 條件三 / pyramid_add.py 條件四) 皆呼叫同一份實作，
# 不會各自漂移出不同天花板值。
# --------------------------------------------------------------------------
def test_deterministic_for_identical_inputs() -> None:
    args = dict(spot=100.0, call_wall=101.0, high_60d=99.0, atr_1d=1.5)
    first = resolve_effective_target(**args)
    second = resolve_effective_target(**args)
    assert first == second
