"""擠壓進場日線事件研究：無前視對齊、前瞻報酬與獨立事件計數。"""

import numpy as np
import pandas as pd

from calibration.squeeze_entry_backtest import (
    align_completed,
    dedupe_events,
    forward_returns,
    summarize,
    three_day_bars,
    weekly_bars,
)


def _daily(start: str, n: int) -> pd.DataFrame:
    idx = pd.bdate_range(start, periods=n)
    close = np.arange(1, n + 1, dtype=float)
    return pd.DataFrame(
        {"Open": close - 0.5, "High": close + 1, "Low": close - 1, "Close": close},
        index=idx,
    )


def test_weekly_bar_is_only_visible_after_week_completes() -> None:
    daily = _daily("2026-09-07", 10)  # 兩個完整週（週一～週五）
    w = weekly_bars(daily)
    aligned = align_completed(w[["Close"]], daily.index)
    # 第一週週四：尚無完成的週線
    assert pd.isna(aligned.loc[pd.Timestamp("2026-09-10"), "Close"])
    # 第一週週五收盤後可用，值為該週收盤
    assert aligned.loc[pd.Timestamp("2026-09-11"), "Close"] == 5.0
    # 第二週週三仍沿用第一週
    assert aligned.loc[pd.Timestamp("2026-09-16"), "Close"] == 5.0


def test_three_day_bars_drop_incomplete_group() -> None:
    t = three_day_bars(_daily("2026-09-07", 8))
    assert len(t) == 2
    assert list(t["Close"]) == [3.0, 6.0]
    assert t.index[-1] == pd.Timestamp("2026-09-14")


def test_forward_returns_enter_next_open() -> None:
    daily = _daily("2026-09-07", 10)
    fwd = forward_returns(daily, horizons=(2,))
    # t=0：t+1 開盤 1.5 進場，t+2 收盤 3.0 出場
    assert fwd["ret_2"].iloc[0] == 3.0 / 1.5 - 1.0
    assert pd.isna(fwd["ret_2"].iloc[-1])


def test_dedupe_events_cooldown() -> None:
    mask = pd.Series([True, True, False, True, False, False, True])
    assert list(dedupe_events(mask, cooldown=2)) == [
        True,
        False,
        False,
        True,
        False,
        False,
        True,
    ]


def test_summarize_counts_independent_events_per_symbol() -> None:
    dates = list(pd.bdate_range("2026-01-05", periods=4)) * 2
    panel = pd.DataFrame(
        {
            "date": dates,
            "symbol": ["A"] * 4 + ["B"] * 4,
            "status": ["ENTRY", "ENTRY", "NONE", "NONE"] * 2,
            "tier": [3, 3, None, None] * 2,
            "at_resistance": [False] * 8,
            "ret_5": [0.1, 0.1, 0.0, 0.0, 0.2, 0.2, 0.0, 0.0],
        }
    )
    stats = {s.label: s for s in summarize(panel, horizons=(5,), cooldown=10)}
    t3 = stats["T3"]
    assert t3.n_events == 4
    assert t3.n_independent == 2  # 每個標的只計第一次
    assert abs(t3.mean - 0.15) < 1e-12
    # 超額 = 報酬 − 同標的平均：A 平均 0.05、B 平均 0.10
    assert abs(t3.excess_mean - ((0.05 * 2 + 0.10 * 2) / 4)) < 1e-12


def test_three_day_bars_reset_each_year() -> None:
    idx = pd.DatetimeIndex(
        [
            "2025-12-29",
            "2025-12-30",
            "2025-12-31",
            "2026-01-02",
            "2026-01-05",
            "2026-01-06",
            "2026-01-07",
        ]
    )
    close = np.arange(1, len(idx) + 1, dtype=float)
    daily = pd.DataFrame(
        {"Open": close, "High": close, "Low": close, "Close": close}, index=idx
    )
    t = three_day_bars(daily)
    # 2025 年三天一組；2026 年從 1/2 重新起算，最後一組（1/7）未滿 3 日丟棄
    assert list(t.index) == [pd.Timestamp("2025-12-31"), pd.Timestamp("2026-01-06")]
