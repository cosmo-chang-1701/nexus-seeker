"""週線 RSI(14) 與日線超賣重返純函式葉模組之單元測試。"""

from __future__ import annotations

import json
from datetime import date, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import numpy as np
import pandas as pd
import pytest

from calibration.advisor_backtest import rsi_wilder
from market_analysis import evaluation_recorder as rec
from market_analysis.rsi_reentry import (
    DAILY_REENTRY_LOOKBACK_BARS,
    REENTRY_NO_DATA,
    REENTRY_NONE,
    REENTRY_OVERSOLD_WATCH,
    REENTRY_TRIGGERED,
    RSI_LENGTH,
    WEEKLY_NEAR_OVERSOLD,
    WEEKLY_NO_DATA,
    WEEKLY_NORMAL,
    WEEKLY_OVERBOUGHT,
    WEEKLY_OVERSOLD,
    RsiReentryStatus,
    compute_rsi_reentry,
    format_rsi_reentry_line,
    wilder_rsi_series,
)
from market_analysis.squeeze_entry import evaluator as ev_mod
from market_analysis.squeeze_entry.rules import evaluate_squeeze_entry
from market_analysis.squeeze_entry.timeframes import TimeframeState


def _daily_df(
    closes: list[float] | np.ndarray,
    start: str = "2025-01-06",
) -> pd.DataFrame:
    idx = pd.bdate_range(start, periods=len(closes))
    c = np.asarray(closes, dtype=float)
    return pd.DataFrame(
        {"Open": c, "High": c + 0.5, "Low": c - 0.5, "Close": c, "Volume": 1e5},
        index=idx,
    )


@pytest.fixture(autouse=True)
def _clean_recorder_buffer() -> None:
    rec._BUFFER.clear()


# ---------------------------------------------------------------------------
# wilder_rsi_series 數學與邊界測試
# ---------------------------------------------------------------------------
def test_wilder_rsi_matches_advisor_backtest() -> None:
    rng = np.random.default_rng(42)
    closes = pd.Series(100.0 + np.cumsum(rng.normal(0.0, 1.5, 80)))
    got = wilder_rsi_series(closes)
    expected = rsi_wilder(closes, period=RSI_LENGTH)
    pd.testing.assert_series_equal(got, expected)


def test_wilder_rsi_flat_all_up_and_all_down_zero() -> None:
    flat = pd.Series([100.0] * 20)
    assert wilder_rsi_series(flat).iloc[-1] == pytest.approx(50.0)

    up = pd.Series(np.arange(100.0, 120.0))
    assert wilder_rsi_series(up).iloc[-1] == pytest.approx(100.0)

    down = pd.Series(np.arange(120.0, 100.0, -1.0))
    assert wilder_rsi_series(down).iloc[-1] == pytest.approx(0.0)


def test_wilder_rsi_nan_and_short_series() -> None:
    assert wilder_rsi_series(pd.Series(dtype=float)).empty
    short = pd.Series(np.arange(1.0, 15.0))  # 14 筆，需要 >= 15 筆才有第一個 RSI
    assert wilder_rsi_series(short).isna().all()

    with_nan = pd.Series([np.nan, *np.arange(100.0, 118.0)])
    with_nan.iloc[5] = np.nan
    res = wilder_rsi_series(with_nan)
    assert np.isnan(res.iloc[0])
    assert np.isnan(res.iloc[5])
    assert np.isfinite(res.iloc[-1])

    with pytest.raises(ValueError):
        wilder_rsi_series(short, length=0)


# ---------------------------------------------------------------------------
# compute_rsi_reentry 索引防護與資料不足
# ---------------------------------------------------------------------------
def test_compute_rsi_reentry_guards_non_datetime_and_missing_close() -> None:
    for bad in (
        None,
        pd.DataFrame(),
        pd.DataFrame(
            {"Open": [1.0, 2.0]}, index=pd.bdate_range("2026-01-02", periods=2)
        ),
        pd.DataFrame({"Close": np.arange(100.0, 130.0)}),  # RangeIndex 沒有 .date()
        pd.DataFrame({"Close": [1.0, 2.0]}, index=["2026-01-01", "2026-01-02"]),
    ):
        st = compute_rsi_reentry(bad, date(2026, 2, 1))
        assert st.weekly_state == WEEKLY_NO_DATA
        assert st.daily_reentry_state == REENTRY_NO_DATA
        assert st.weekly_rsi is None
        assert st.weekly_is_oversold is None
        assert st.daily_rsi is None
        assert st.daily_prev_rsi is None
        assert st.daily_above_sma20 is None


def test_compute_rsi_reentry_insufficient_bars() -> None:
    # 15 根已收盤日線 → 週線只有 3 根（不足 15）、日線 15 根（不足 16 算 prev_rsi）
    df = _daily_df(np.linspace(100.0, 110.0, 15), start="2026-01-05")
    st = compute_rsi_reentry(df, date(2026, 2, 1))
    assert st.weekly_state == WEEKLY_NO_DATA
    assert st.daily_reentry_state == REENTRY_NO_DATA
    assert st.daily_above_sma20 is None


# ---------------------------------------------------------------------------
# 週線重採樣（週五收盤邊界、即時週預覽與四區段）
# ---------------------------------------------------------------------------
def test_weekly_friday_boundary_and_live_preview() -> None:
    # 20 週完整週線（週一~週五）+ 第 21 週週一~週三急漲
    dates = pd.bdate_range("2025-09-01", "2026-01-21")  # 2026-01-21 為週三
    closes = np.linspace(150.0, 100.0, len(dates))
    closes[-3:] = [105.0, 110.0, 115.0]  # 僅存在於未收盤第 21 週
    df = _daily_df(closes)
    df.index = dates

    # 週四評估：第 21 週尚未結束，weekly_rsi 只用前 20 週（下跌 → 超賣），live 較高
    st_thu = compute_rsi_reentry(df, date(2026, 1, 22))
    assert st_thu.weekly_rsi == pytest.approx(0.0)
    assert st_thu.weekly_state == WEEKLY_OVERSOLD
    assert st_thu.weekly_is_oversold is True
    assert st_thu.weekly_rsi_live is not None
    assert st_thu.weekly_rsi_live > 0.0

    # 補滿週四、週五並在週六評估：第 21 週已確認，live 應為 None
    dates_full = pd.bdate_range("2025-09-01", "2026-01-23")
    closes_full = np.concatenate([closes, [118.0, 120.0]])
    df_full = _daily_df(closes_full)
    df_full.index = dates_full
    st_sat = compute_rsi_reentry(df_full, date(2026, 1, 24))
    assert st_sat.weekly_rsi is not None and st_sat.weekly_rsi > 0.0
    assert st_sat.weekly_rsi_live is None


def test_weekly_rsi_all_bands() -> None:
    # 用可控週線序列（每週五天同收盤價）驗證四個區段門檻
    def _weekly_const_daily(weekly_closes: list[float]) -> tuple[pd.DataFrame, date]:
        all_dates: list[pd.Timestamp] = []
        all_closes: list[float] = []
        start = pd.Timestamp("2025-01-06")
        for i, wc in enumerate(weekly_closes):
            mon = start + pd.Timedelta(weeks=i)
            for d in range(5):
                all_dates.append(mon + pd.Timedelta(days=d))
                all_closes.append(wc)
        df = _daily_df(all_closes)
        df.index = pd.DatetimeIndex(all_dates)
        return df, (all_dates[-1] + pd.Timedelta(days=2)).date()

    # OVERBOUGHT (>= 70)
    df_ob, today_ob = _weekly_const_daily([100.0 + i * 2.0 for i in range(20)])
    st_ob = compute_rsi_reentry(df_ob, today_ob)
    assert st_ob.weekly_state == WEEKLY_OVERBOUGHT
    assert st_ob.weekly_is_oversold is False

    # OVERSOLD (<= 30)
    df_os, today_os = _weekly_const_daily([200.0 - i * 2.0 for i in range(20)])
    st_os = compute_rsi_reentry(df_os, today_os)
    assert st_os.weekly_state == WEEKLY_OVERSOLD
    assert st_os.weekly_is_oversold is True

    # NEAR_OVERSOLD (30 < RSI <= 35) 與 NORMAL (35 < RSI < 70)
    # 構造先跌 14 週再反彈，微調最後一週收盤以分別落在 (30, 35] 與 (35, 70)
    base_down = [200.0 - i * 2.0 for i in range(15)] + [170.0, 171.0, 172.0, 173.0]
    found_near = False
    found_norm = False
    for tail in np.linspace(170.0, 210.0, 81):
        df_t, today_t = _weekly_const_daily([*base_down, float(tail)])
        st_t = compute_rsi_reentry(df_t, today_t)
        if st_t.weekly_state == WEEKLY_NEAR_OVERSOLD:
            assert st_t.weekly_rsi is not None
            assert 30.0 < st_t.weekly_rsi <= 35.0
            assert st_t.weekly_is_oversold is False
            found_near = True
        elif st_t.weekly_state == WEEKLY_NORMAL:
            assert st_t.weekly_rsi is not None
            assert 35.0 < st_t.weekly_rsi < 70.0
            assert st_t.weekly_is_oversold is False
            found_norm = True
    assert found_near and found_norm


# ---------------------------------------------------------------------------
# 日線重返狀態機：OVERSOLD_WATCH / TRIGGERED / lookback 邊界 / NONE
# ---------------------------------------------------------------------------
def _series_with_tail_rsi(
    head_len: int, tail_deltas: list[float]
) -> tuple[pd.DataFrame, date]:
    # 先跌出深度超賣，再用 tail_deltas 控制最後數根的 RSI 走勢
    closes: list[float] = [200.0]
    for _ in range(head_len):
        closes.append(closes[-1] - 2.0)
    for d in tail_deltas:
        closes.append(closes[-1] + d)
    df = _daily_df(closes, start="2025-06-02")
    today = (df.index[-1] + pd.Timedelta(days=3)).date()
    return df, today


def test_daily_reentry_oversold_watch_and_triggered_boundaries() -> None:
    # 1. 仍在 <= 30（含連跌 RSI=0.0）→ OVERSOLD_WATCH，不是 TRIGGERED
    df_watch, today_w = _series_with_tail_rsi(20, [])
    st_watch = compute_rsi_reentry(df_watch, today_w)
    assert st_watch.daily_rsi == pytest.approx(0.0)
    assert st_watch.daily_reentry_state == REENTRY_OVERSOLD_WATCH
    assert st_watch.daily_reentry_bars_since_oversold is None
    assert st_watch.daily_above_sma20 is False

    # 2. 1 根前 <= 30，當根反彈跨越 > 30 且高於前一根 → TRIGGERED (bars_since=1)
    # head=20 後 avg_loss=2.0，若上漲 d 使得 d/(2*13+d) > 3/7 → d > 11.143，取 +14.0
    df_t1, today_t1 = _series_with_tail_rsi(20, [14.0])
    st_t1 = compute_rsi_reentry(df_t1, today_t1)
    assert st_t1.daily_prev_rsi is not None and st_t1.daily_prev_rsi <= 30.0
    assert st_t1.daily_rsi is not None and st_t1.daily_rsi > 30.0
    assert st_t1.daily_reentry_state == REENTRY_TRIGGERED
    assert st_t1.daily_reentry_bars_since_oversold == 1

    # 3. 恰好在回看窗口第 5 根邊界（offset=5）時超賣，之後連漲 → TRIGGERED (bars_since=5)
    assert DAILY_REENTRY_LOOKBACK_BARS == 5
    df_t5, today_t5 = _series_with_tail_rsi(20, [13.0, 0.5, 0.5, 0.5, 0.5])
    st_t5 = compute_rsi_reentry(df_t5, today_t5)
    assert st_t5.daily_reentry_state == REENTRY_TRIGGERED
    assert st_t5.daily_reentry_bars_since_oversold == 5

    # 4. 超過回看窗口（第 6 根前才超賣）→ NONE
    df_t6, today_t6 = _series_with_tail_rsi(20, [13.0, 0.5, 0.5, 0.5, 0.5, 0.5])
    st_t6 = compute_rsi_reentry(df_t6, today_t6)
    assert st_t6.daily_reentry_state == REENTRY_NONE
    assert st_t6.daily_reentry_bars_since_oversold is None

    # 5. 回看窗口內曾超賣且當根 > 30，但當根較前一根走低（拉回）→ NONE
    df_pull, today_p = _series_with_tail_rsi(20, [15.0, -0.5])
    st_pull = compute_rsi_reentry(df_pull, today_p)
    assert st_pull.daily_rsi is not None and st_pull.daily_rsi > 30.0
    assert (
        st_pull.daily_prev_rsi is not None
        and st_pull.daily_rsi < st_pull.daily_prev_rsi
    )
    assert st_pull.daily_reentry_state == REENTRY_NONE


def test_compute_rsi_reentry_drops_today_bar() -> None:
    df, _ = _series_with_tail_rsi(20, [14.0])
    # 把最後一根日期當作 today → 被 split_daily_confirmed 排除，只剩連跌段 (OVERSOLD_WATCH)
    today_is_last = df.index[-1].date()
    st = compute_rsi_reentry(df, today_is_last)
    assert st.daily_reentry_state == REENTRY_OVERSOLD_WATCH
    assert st.daily_rsi == pytest.approx(0.0)


# ---------------------------------------------------------------------------
# format_rsi_reentry_line 格式化測試
# ---------------------------------------------------------------------------
def test_format_rsi_reentry_line_formatting_and_budget() -> None:
    assert format_rsi_reentry_line(None) is None
    no_data = compute_rsi_reentry(None, date(2026, 2, 1))
    assert format_rsi_reentry_line(no_data) is None

    for w_state, w_val in (
        (WEEKLY_OVERSOLD, 28.4),
        (WEEKLY_NEAR_OVERSOLD, 33.2),
        (WEEKLY_NORMAL, 52.1),
        (WEEKLY_OVERBOUGHT, 74.8),
        (WEEKLY_NO_DATA, None),
    ):
        for d_state in (
            REENTRY_TRIGGERED,
            REENTRY_OVERSOLD_WATCH,
            REENTRY_NONE,
            REENTRY_NO_DATA,
        ):
            if w_state == WEEKLY_NO_DATA and d_state == REENTRY_NO_DATA:
                continue
            line = format_rsi_reentry_line(
                RsiReentryStatus(
                    weekly_rsi=w_val,
                    weekly_rsi_live=None,
                    weekly_state=w_state,
                    weekly_is_oversold=(w_state == WEEKLY_OVERSOLD)
                    if w_val is not None
                    else None,
                    daily_rsi=34.5,
                    daily_prev_rsi=29.0,
                    daily_reentry_state=d_state,
                    daily_above_sma20=True,
                    daily_reentry_bars_since_oversold=1
                    if d_state == REENTRY_TRIGGERED
                    else None,
                )
            )
            assert line is not None
            assert "\x1b[" not in line
            assert len(line) <= 65


# ---------------------------------------------------------------------------
# evaluate_symbol 與 evaluation_recorder 整合測試
# ---------------------------------------------------------------------------
async def test_evaluate_symbol_attaches_rsi_reentry_without_changing_result() -> None:
    df_daily, today = _series_with_tail_rsi(110, [14.0])
    now_ny = datetime.combine(today, datetime.min.time()).replace(hour=11, minute=0)
    d_state = TimeframeState(
        timeframe="D",
        squeeze_level="Normal",
        is_squeezing=True,
        momentum_value=1.2,
        momentum_color="LightBlue",
        green_dot=True,
        green_dot_bars_ago=0,
        turbo=False,
        squeeze_range_low=95.0,
        sma_20=98.0,
        last_close=100.0,
        bar_ts="2026-01-01",
    )
    matrix = {"D": d_state}
    with patch(
        "market_analysis.squeeze_entry.evaluator.fetch_psq_matrix",
        AsyncMock(return_value=(matrix, df_daily)),
    ):
        ev = await ev_mod.evaluate_symbol("NVDA", 100.0, now_ny=now_ny)

    expected_result = evaluate_squeeze_entry(matrix, ev.resistance)
    assert ev.result == expected_result
    assert ev.rsi_reentry is not None
    assert ev.rsi_reentry.daily_reentry_state == REENTRY_TRIGGERED


def test_record_squeeze_entry_preserves_zero_and_stays_under_max() -> None:
    st_zero = RsiReentryStatus(
        weekly_rsi=0.0,
        weekly_rsi_live=None,
        weekly_state=WEEKLY_OVERSOLD,
        weekly_is_oversold=True,
        daily_rsi=0.0,
        daily_prev_rsi=0.0,
        daily_reentry_state=REENTRY_OVERSOLD_WATCH,
        daily_above_sma20=False,
        daily_reentry_bars_since_oversold=None,
    )
    res = SimpleNamespace(
        status="WATCH",
        tier=None,
        size_pct=None,
        squeeze_count=2,
        triggers=[],
        stop=None,
        reason="WATCH",
    )
    with rec.evaluation_source("WATCHLIST_ADVISOR"):
        rec.record_squeeze_entry("AAA", 100.0, res, {}, rsi_reentry=st_zero)

    row = rec._BUFFER[0]
    feats = json.loads(row["features_json"])
    assert feats["w_rsi_14"] == 0.0
    assert feats["w_rsi_os"] is True
    assert feats["d_rsi_14"] == 0.0
    assert feats["d_reentry"] == REENTRY_OVERSOLD_WATCH
    assert len(row["features_json"]) <= rec._FEATURES_JSON_MAX
