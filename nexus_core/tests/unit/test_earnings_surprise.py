"""單元測試：財務預期差綜合評分量化模組 (earnings_surprise.py)。"""

from __future__ import annotations

import pytest
from market_analysis.fundamental_pipeline.earnings_surprise import (
    EPS_SURPRISE_CLIP,
    REV_SURPRISE_CLIP,
    calculate_composite_surprise_score,
    calculate_eps_surprise,
    calculate_revenue_surprise,
    calculate_whisper_surprise,
    evaluate_earnings_surprise,
)


def test_calculate_eps_surprise_normal() -> None:
    """測試正常每股盈餘預期差計算。"""
    # 實際 1.10，共識 1.00 -> +10.0%
    surprise, small_base = calculate_eps_surprise(1.10, 1.00)
    assert surprise is not None
    assert pytest.approx(surprise, 0.001) == 0.10
    assert small_base is False

    # 實際 0.80，共識 1.00 -> -20.0%
    surprise_neg, small_base_neg = calculate_eps_surprise(0.80, 1.00)
    assert surprise_neg is not None
    assert pytest.approx(surprise_neg, 0.001) == -0.20
    assert small_base_neg is False


def test_calculate_eps_surprise_small_base() -> None:
    """測試分母接近零保護門檻 (FLOOR_EPS = 0.05)。"""
    # 共識 0.02 (小於 0.05)，實際 0.04
    # 分母強制使用 0.05，差值 0.02 -> 0.02 / 0.05 = 0.40 (+40.0%)
    surprise, small_base = calculate_eps_surprise(0.04, 0.02)
    assert surprise is not None
    assert pytest.approx(surprise, 0.001) == 0.40
    assert small_base is True

    # 負微小值共識 -0.01
    # 分母 max(0.01, 0.05) = 0.05，差值 0.01 - (-0.01) = 0.02 -> 0.40
    surprise_neg, small_base_neg = calculate_eps_surprise(0.01, -0.01)
    assert surprise_neg is not None
    assert pytest.approx(surprise_neg, 0.001) == 0.40
    assert small_base_neg is True


def test_calculate_eps_surprise_clipping() -> None:
    """測試每股盈餘預期差雙向箝制於 ±50%。"""
    # 暴增 +100% 箝制為 +50%
    surprise_high, _ = calculate_eps_surprise(2.50, 1.00)
    assert surprise_high == EPS_SURPRISE_CLIP

    # 暴跌 -100% 箝制為 -50%
    surprise_low, _ = calculate_eps_surprise(-0.50, 1.00)
    assert surprise_low == -EPS_SURPRISE_CLIP


def test_calculate_eps_surprise_none_inputs() -> None:
    """測試缺失輸入時安全回傳 None。"""
    s1, b1 = calculate_eps_surprise(None, 1.00)
    assert s1 is None
    assert b1 is False

    s2, b2 = calculate_eps_surprise(1.00, None)
    assert s2 is None
    assert b2 is False


def test_calculate_revenue_surprise_normal_and_clip() -> None:
    """測試營收預期差正常計算與 ±10% 箝制。"""
    # 正常 +5%
    rev_s = calculate_revenue_surprise(105.0, 100.0)
    assert rev_s is not None
    assert pytest.approx(rev_s, 0.001) == 0.05

    # 超過 10% 箝制
    rev_high = calculate_revenue_surprise(125.0, 100.0)
    assert rev_high == REV_SURPRISE_CLIP

    # 暴跌超過 -10% 箝制
    rev_low = calculate_revenue_surprise(70.0, 100.0)
    assert rev_low == -REV_SURPRISE_CLIP

    # 無效共識或負共識
    assert calculate_revenue_surprise(100.0, 0.0) is None
    assert calculate_revenue_surprise(100.0, -50.0) is None
    assert calculate_revenue_surprise(None, 100.0) is None


def test_calculate_whisper_surprise() -> None:
    """測試耳語預期差計算。"""
    whisp_s, small_base = calculate_whisper_surprise(1.20, 1.00)
    assert whisp_s is not None
    assert pytest.approx(whisp_s, 0.001) == 0.20
    assert small_base is False


def test_calculate_composite_surprise_score_both_present() -> None:
    """測試雙指標皆存在時之標準加權評分 (EPS 60%, REV 40%)。"""
    # EPS 剛好達 +50% 上限 (貢獻 60 分)，REV 剛好達 +10% 上限 (貢獻 40 分) -> 總分 100
    score_max = calculate_composite_surprise_score(0.50, 0.10)
    assert score_max == 100.0

    # EPS -50% (貢獻 -60 分)，REV -10% (貢獻 -40 分) -> 總分 -100
    score_min = calculate_composite_surprise_score(-0.50, -0.10)
    assert score_min == -100.0

    # EPS +25% (0.25 / 0.50 * 60 = 30 分)，REV +5% (0.05 / 0.10 * 40 = 20 分) -> 總分 50
    score_mid = calculate_composite_surprise_score(0.25, 0.05)
    assert score_mid is not None
    assert pytest.approx(score_mid, 0.001) == 50.0


def test_calculate_composite_surprise_score_single_metric_renormalization() -> None:
    """測試單項數據缺失時權重自動正規化為 1.0。"""
    # 只有 EPS +25% -> 0.25 / 0.50 * 100 = 50.0
    score_eps_only = calculate_composite_surprise_score(0.25, None)
    assert score_eps_only is not None
    assert pytest.approx(score_eps_only, 0.001) == 50.0

    # 只有 REV +5% -> 0.05 / 0.10 * 100 = 50.0
    score_rev_only = calculate_composite_surprise_score(None, 0.05)
    assert score_rev_only is not None
    assert pytest.approx(score_rev_only, 0.001) == 50.0

    # 兩者皆無回傳 None
    assert calculate_composite_surprise_score(None, None) is None


def test_calculate_composite_surprise_score_with_whisper() -> None:
    """測試納入耳語預期差時之混合計算。"""
    # EPS +50%, Whisper +50%, REV +10%, whisper_weight = 0.2
    score = calculate_composite_surprise_score(
        eps_surprise=0.50,
        rev_surprise=0.10,
        whisper_surprise=0.50,
        whisper_weight=0.20,
    )
    assert score == 100.0


def test_evaluate_earnings_surprise_full() -> None:
    """測試完整 evaluate_earnings_surprise 回傳之資料結構。"""
    result = evaluate_earnings_surprise(
        actual_eps=1.10,
        consensus_eps=1.00,
        actual_revenue=102.0,
        consensus_revenue=100.0,
        whisper_eps=1.05,
        whisper_weight=0.10,
    )
    assert result.eps_surprise_pct is not None
    assert pytest.approx(result.eps_surprise_pct, 0.001) == 0.10
    assert result.revenue_surprise_pct is not None
    assert pytest.approx(result.revenue_surprise_pct, 0.001) == 0.02
    assert result.whisper_surprise_pct is not None
    assert pytest.approx(result.whisper_surprise_pct, 0.001) == 0.0476
    assert result.composite_score is not None
    assert result.small_base is False
