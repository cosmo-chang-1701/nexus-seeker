"""事後走勢標註 (market_analysis/outcome_labeling.py) 單元測試：合成 K 線、無網路。"""

from datetime import datetime, timezone

import pandas as pd
import pytest

from market_analysis.outcome_labeling import (
    TOUCH_BOTH_SAME_BAR,
    TOUCH_DOWN,
    TOUCH_NONE,
    TOUCH_UP,
    directional_touch,
    first_barrier_touch,
    label_forward_path,
    plan_outcome,
)

# 2026-03-02 (週一) 14:30 UTC = 09:30 ET
_ENTRY = datetime(2026, 3, 2, 15, 0, tzinfo=timezone.utc)


def _hourly_bars(closes_by_day: list[list[float]], spread: float = 0.2) -> pd.DataFrame:
    """每個交易日 7 根 1h K 棒 (14:30–20:30 UTC)，從 2026-03-02 起連續平日。"""
    rows = []
    day = pd.Timestamp("2026-03-02", tz="UTC")
    for closes in closes_by_day:
        while day.dayofweek >= 5:
            day += pd.Timedelta(days=1)
        for i, c in enumerate(closes):
            ts = day + pd.Timedelta(hours=14, minutes=30) + pd.Timedelta(hours=i)
            rows.append((ts, c, c + spread, c - spread))
        day += pd.Timedelta(days=1)
    idx = pd.DatetimeIndex([r[0] for r in rows])
    return pd.DataFrame(
        {
            "Open": [r[1] for r in rows],
            "High": [r[2] for r in rows],
            "Low": [r[3] for r in rows],
            "Close": [r[1] for r in rows],
        },
        index=idx,
    )


class TestFirstBarrierTouch:
    def test_up_first(self) -> None:
        assert first_barrier_touch([101, 106], [99, 104], 105, 95) == (TOUCH_UP, 2)

    def test_down_first(self) -> None:
        assert first_barrier_touch([101, 101], [96, 94], 105, 95) == (TOUCH_DOWN, 2)

    def test_same_bar_both(self) -> None:
        assert first_barrier_touch([106], [94], 105, 95) == (TOUCH_BOTH_SAME_BAR, 1)

    def test_timeout(self) -> None:
        assert first_barrier_touch([101], [99], 105, 95) == (TOUCH_NONE, None)


class TestDirectionalTouch:
    @pytest.mark.parametrize(
        ("touch", "direction", "expected"),
        [
            (TOUCH_UP, "LONG", 1),
            (TOUCH_DOWN, "LONG", -1),
            (TOUCH_DOWN, "SHORT", 1),
            (TOUCH_UP, "SHORT", -1),
            (TOUCH_BOTH_SAME_BAR, "SHORT", -1),  # 同根雙觸一律不利
            (TOUCH_BOTH_SAME_BAR, "LONG", -1),
            (TOUCH_NONE, "LONG", 0),
        ],
    )
    def test_mapping(self, touch: int, direction: str, expected: int) -> None:
        assert directional_touch(touch, direction) == expected


class TestLabelForwardPath:
    def test_excludes_bars_at_or_before_entry(self) -> None:
        """entry 所在那根 (含評估前價格) 不得納入：第一根的 High 120 若被納入，
        +1×ATR 會被誤判為先觸及。"""
        bars = _hourly_bars([[100.0] * 7] * 6)
        bars.loc[bars.index[0], "High"] = 120.0  # 14:30 那根，早於 entry 15:00
        label = label_forward_path(bars, _ENTRY, 100.0, atr_1d=5.0)
        assert label is not None
        assert label.max_up_atr_5d == pytest.approx(0.04)  # (100.2 − 100)/5
        assert all(t.touch == TOUCH_NONE for t in label.touches)

    def test_horizons_and_touches(self) -> None:
        # 進場日持平；隔日跌至 94；第 3 日 90；第 5 日 88
        days = [
            [100.0] * 7,
            [96.0, 95.0, 94.0, 94.0, 94.0, 94.0, 94.0],
            [92.0] * 7,
            [90.0] * 7,
            [89.0] * 7,
            [88.0] * 7,
        ]
        label = label_forward_path(_hourly_bars(days), _ENTRY, 100.0, atr_1d=5.0)
        assert label is not None
        assert label.fwd_ret_eod == pytest.approx(0.0)
        assert label.fwd_ret_1d == pytest.approx(-0.06)
        assert label.fwd_ret_3d == pytest.approx(-0.10)
        assert label.fwd_ret_5d == pytest.approx(-0.12)
        assert label.fwd_ret_1h == pytest.approx(0.0)
        by_k = {t.k: t for t in label.touches}
        assert by_k[1.0].touch == TOUCH_DOWN
        assert by_k[2.0].touch == TOUCH_DOWN  # 88 − 0.2 <= 90
        assert label.max_down_atr_5d == pytest.approx((100.0 - 87.8) / 5.0)

    def test_incomplete_window_leaves_later_horizons_none(self) -> None:
        label = label_forward_path(
            _hourly_bars([[100.0] * 7, [101.0] * 7]), _ENTRY, 100.0, atr_1d=5.0
        )
        assert label is not None
        assert label.fwd_ret_1d == pytest.approx(0.01)
        assert label.fwd_ret_3d is None
        assert label.fwd_ret_5d is None

    @pytest.mark.parametrize("atr", [0.0, float("nan")])
    def test_invalid_atr_returns_none(self, atr: float) -> None:
        assert (
            label_forward_path(_hourly_bars([[100.0] * 7]), _ENTRY, 100.0, atr) is None
        )

    def test_no_bars_after_entry(self) -> None:
        late = datetime(2026, 3, 9, 0, 0, tzinfo=timezone.utc)
        assert label_forward_path(_hourly_bars([[100.0] * 7]), late, 100.0, 5.0) is None


class TestPlanOutcome:
    def test_short_target_first(self) -> None:
        bars = _hourly_bars([[100.0] * 7, [94.0] * 7])
        assert (
            plan_outcome(bars, _ENTRY, "SHORT", stop_price=105.0, target_price=95.0)
            == 1
        )

    def test_short_stop_first(self) -> None:
        bars = _hourly_bars([[100.0] * 7, [106.0] * 7])
        assert plan_outcome(bars, _ENTRY, "SHORT", 105.0, 95.0) == -1

    def test_long_neither(self) -> None:
        bars = _hourly_bars([[100.0] * 7, [101.0] * 7])
        assert plan_outcome(bars, _ENTRY, "LONG", 95.0, 110.0) == 0

    def test_missing_levels(self) -> None:
        assert (
            plan_outcome(_hourly_bars([[100.0] * 7]), _ENTRY, "LONG", None, 110.0)
            is None
        )


def test_naive_bar_index_is_treated_as_us_eastern() -> None:
    """get_history_df 回傳去掉時區的美東時間。10:30 ET 的 K 棒 (= 14:30/15:30 UTC)
    若被誤當 UTC 10:30，會被視為早於 15:00 UTC 的評估時點而遭排除。"""
    idx = pd.DatetimeIndex(["2026-03-02 10:30", "2026-03-02 11:30", "2026-03-03 10:30"])
    bars = pd.DataFrame(
        {"Open": 100.0, "High": [120.0, 100.2, 100.2], "Low": 99.8, "Close": 100.0},
        index=idx,
    )
    entry = datetime(2026, 3, 2, 15, 0, tzinfo=timezone.utc)  # = 10:00 ET
    label = label_forward_path(bars, entry, 100.0, atr_1d=5.0)
    assert label is not None
    assert label.max_up_atr_5d == pytest.approx(4.0)  # 10:30 ET 那根 High 120 被納入
