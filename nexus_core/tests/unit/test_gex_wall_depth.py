"""is_wall_depth_sufficient：ADV 門檻＋profile 最大正淨 GEX 相對門檻（純函式）。"""

from market_analysis.gex_wall_depth import (
    CALLWALL_MIN_PEAK_RATIO,
    is_wall_depth_sufficient,
)


def test_unknown_depth_returns_none() -> None:
    assert is_wall_depth_sufficient(None, 3.04e8, 5.46e9) is None


def test_voo_790_rejected_by_relative_rule_without_adv() -> None:
    # 事故實況：ADV 未注入 → 絕對下限 500K 擋不住 2.04e6，相對門檻 3.04e6 擋下。
    assert is_wall_depth_sufficient(2_041_178.28, 303_526_351.95, None) is False


def test_voo_790_rejected_by_adv_threshold() -> None:
    assert is_wall_depth_sufficient(2_041_178.28, None, 5.46e9) is False


def test_deep_wall_passes() -> None:
    assert is_wall_depth_sufficient(303_526_351.95, 303_526_351.95, 5.46e9) is True


def test_absolute_floor_without_peak() -> None:
    assert is_wall_depth_sufficient(600_000.0, None, None) is True
    assert is_wall_depth_sufficient(400_000.0, None, None) is False


def test_ratio_constant() -> None:
    assert CALLWALL_MIN_PEAK_RATIO == 0.01
