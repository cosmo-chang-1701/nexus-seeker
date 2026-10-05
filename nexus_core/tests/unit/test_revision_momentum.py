"""分析師修正動能模型與 PEAD 捕捉單元測試。"""

import pytest
from market_analysis.fundamental_pipeline.models import (
    EPSEstimateSnapshotRecord,
)
from market_analysis.fundamental_pipeline.revision_momentum import (
    FLOOR_EPS,
    calculate_breadth,
    calculate_horizon_slope,
    calculate_revision_score,
    evaluate_revision_momentum,
    is_pead_aligned,
)


def test_calculate_horizon_slope_normal() -> None:
    # 正常上修: 2.0 -> 2.5, 斜率 = (2.5 - 2.0) / 2.0 = 0.25
    slope = calculate_horizon_slope(2.5, 2.0)
    assert pytest.approx(slope, rel=1e-4) == 0.25

    # 正常下修: 2.0 -> 1.6, 斜率 = (1.6 - 2.0) / 2.0 = -0.20
    slope_down = calculate_horizon_slope(1.6, 2.0)
    assert pytest.approx(slope_down, rel=1e-4) == -0.20


def test_calculate_horizon_slope_floor_eps() -> None:
    # prior_eps 接近零，觸發 FLOOR_EPS = 0.05 防護
    slope_near_zero = calculate_horizon_slope(0.10, 0.01)
    assert pytest.approx(slope_near_zero, rel=1e-4) == (0.10 - 0.01) / FLOOR_EPS

    # 負數 prior_eps 絕對值小於 0.05
    slope_neg_near_zero = calculate_horizon_slope(0.05, -0.02)
    assert pytest.approx(slope_neg_near_zero, rel=1e-4) == (0.05 - (-0.02)) / FLOOR_EPS


def test_calculate_breadth() -> None:
    # 全部調升: 4 up, 0 down -> 1.0
    assert pytest.approx(calculate_breadth(4, 0), rel=1e-4) == 1.0

    # 全部調降: 0 up, 4 down -> -1.0
    assert pytest.approx(calculate_breadth(0, 4), rel=1e-4) == -1.0

    # 對半調升調降: 2 up, 2 down -> 0.0
    assert pytest.approx(calculate_breadth(2, 2), rel=1e-4) == 0.0

    # 無任何調整
    assert calculate_breadth(0, 0) == 0.0


def test_calculate_revision_score() -> None:
    # 滿分調升 (所有 horizon 斜率 >= 0.20, breadth = 1.0)
    slopes = {"0q": 0.25, "+1q": 0.30, "0y": 0.20, "+1y": 0.22}
    score_max = calculate_revision_score(slopes, breadth=1.0)
    assert pytest.approx(score_max, rel=1e-2) == 100.0

    # 極端調降 (所有 horizon 斜率 <= -0.20, breadth = -1.0)
    slopes_down = {"0q": -0.25, "+1q": -0.30, "0y": -0.20, "+1y": -0.22}
    score_min = calculate_revision_score(slopes_down, breadth=-1.0)
    assert pytest.approx(score_min, rel=1e-2) == -100.0

    # 部分期限缺失重新正規化
    slopes_partial = {"0q": 0.20, "+1q": 0.20}
    score_partial = calculate_revision_score(slopes_partial, breadth=0.5)
    # 0.70 * 1.0 + 0.30 * 0.5 = 0.85 -> 85.0
    assert pytest.approx(score_partial, rel=1e-2) == 85.0

    # 完全無可用斜率
    score_empty = calculate_revision_score({}, breadth=0.0)
    assert score_empty == 0.0


def test_is_pead_aligned() -> None:
    # 正向共振: surprise = 25.0, revision = 30.0, 15 天 -> True
    assert is_pead_aligned(25.0, 30.0, 15) is True

    # 負向共振: surprise = -20.0, revision = -25.0, 40 天 -> True
    assert is_pead_aligned(-20.0, -25.0, 40) is True

    # 方向不一致: surprise = +25.0, revision = -30.0 -> False
    assert is_pead_aligned(25.0, -30.0, 15) is False

    # 顯著性不足: surprise = +10.0 (< 15.0) -> False
    assert is_pead_aligned(10.0, 30.0, 15) is False

    # 超過 60 交易日上限 -> False
    assert is_pead_aligned(30.0, 30.0, 65) is False

    # 缺值保護 -> False
    assert is_pead_aligned(None, 30.0, 15) is False
    assert is_pead_aligned(30.0, None, 15) is False
    assert is_pead_aligned(30.0, 30.0, None) is False


def test_evaluate_revision_momentum_end_to_end() -> None:
    curr_snaps = [
        EPSEstimateSnapshotRecord(
            symbol="NVDA",
            snapshot_date="2026-10-05",
            horizon="0q",
            source="finnhub",
            eps_mean=0.90,
        ),
        EPSEstimateSnapshotRecord(
            symbol="NVDA",
            snapshot_date="2026-10-05",
            horizon="+1q",
            source="finnhub",
            eps_mean=1.05,
        ),
        EPSEstimateSnapshotRecord(
            symbol="NVDA",
            snapshot_date="2026-10-05",
            horizon="0y",
            source="finnhub",
            eps_mean=3.80,
        ),
        EPSEstimateSnapshotRecord(
            symbol="NVDA",
            snapshot_date="2026-10-05",
            horizon="+1y",
            source="finnhub",
            eps_mean=4.60,
        ),
    ]

    prior_snaps = [
        EPSEstimateSnapshotRecord(
            symbol="NVDA",
            snapshot_date="2026-09-05",
            horizon="0q",
            source="finnhub",
            eps_mean=0.80,
        ),
        EPSEstimateSnapshotRecord(
            symbol="NVDA",
            snapshot_date="2026-09-05",
            horizon="+1q",
            source="finnhub",
            eps_mean=0.95,
        ),
        EPSEstimateSnapshotRecord(
            symbol="NVDA",
            snapshot_date="2026-09-05",
            horizon="0y",
            source="finnhub",
            eps_mean=3.50,
        ),
        EPSEstimateSnapshotRecord(
            symbol="NVDA",
            snapshot_date="2026-09-05",
            horizon="+1y",
            source="finnhub",
            eps_mean=4.20,
        ),
    ]

    res = evaluate_revision_momentum(
        current_snapshots=curr_snaps,
        prior_snapshots=prior_snaps,
        surprise_score=35.0,
        days_since_surprise=20,
    )

    assert res.score_30d > 0
    assert res.breadth_ratio == 1.0
    assert res.is_pead_aligned is True
    assert len(res.slopes) == 4
    assert res.up_count == 4
    assert res.down_count == 0
