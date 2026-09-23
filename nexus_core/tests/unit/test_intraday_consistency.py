"""
tests/unit/test_intraday_consistency.py

`/x` 日內資料一致性閘門：日高低點校正、VWAP 區間不變式、15m K 棒凍結／合併
偵測、期權成交價無套利下界。fixture 取自實盤回報的矛盾輸出（SNDK / MU）。
"""

from datetime import date, datetime
from typing import Any
from unittest.mock import AsyncMock, patch

import pandas as pd
import pytest

from market_analysis.intraday_consistency import (
    BarAssessment,
    assess_15m_bar,
    is_vwap_within_range,
    reconcile_daily_range,
    sanitize_option_trade_price,
)
from market_analysis.vwap_utils import fetch_session_stats

TODAY = date(2026, 9, 22)


# ── 日高低點校正 ──────────────────────────────────────────────


def test_iex_narrow_high_is_widened_by_consolidated_bars() -> None:
    """SNDK 第 1 版：IEX 日高 1791.11，但全市場 K 線已到 1795 → VWAP 1791.74 不再超出區間。"""
    quote = {"c": 1789.42, "h": 1791.11, "l": 1756.82, "o": 1760.0}
    fixed, changed = reconcile_daily_range(quote, 1795.00, 1754.50, TODAY, TODAY)
    assert changed
    assert fixed["h"] == pytest.approx(1795.00)
    # MU/SNDK 第 2 版：15m 低點 1754.50 < 全日低 1756.82 → 低點亦被放寬
    assert fixed["l"] == pytest.approx(1754.50)
    assert is_vwap_within_range(1791.74, fixed["h"], fixed["l"])
    # 輸入 dict 不被修改
    assert quote["h"] == 1791.11


def test_range_is_never_narrowed() -> None:
    quote = {"c": 100.0, "h": 105.0, "l": 95.0}
    fixed, changed = reconcile_daily_range(quote, 103.0, 97.0, TODAY, TODAY)
    assert not changed
    assert fixed is quote


def test_previous_session_bars_are_not_merged() -> None:
    """盤前 yfinance period=1d 回傳昨天的 K 線，不得混入今天的區間。"""
    quote = {"c": 100.0, "h": 101.0, "l": 99.0}
    fixed, changed = reconcile_daily_range(quote, 120.0, 80.0, date(2026, 9, 21), TODAY)
    assert not changed
    assert fixed["h"] == 101.0


def test_current_price_always_inside_range() -> None:
    quote = {"c": 1881.0, "h": 1800.0, "l": 1750.0}
    fixed, changed = reconcile_daily_range(quote, None, None, None, TODAY)
    assert changed
    assert fixed["h"] == pytest.approx(1881.0)


def test_vwap_outside_range_is_rejected() -> None:
    assert not is_vwap_within_range(1791.74, 1791.11, 1756.82)
    assert is_vwap_within_range(1780.0, 1791.11, 1756.82)
    assert not is_vwap_within_range(None, 1791.11, 1756.82)


# ── 15m K 棒 ─────────────────────────────────────────────────


def _assess(**overrides: object) -> BarAssessment:
    kwargs: dict[str, Any] = dict(
        bar_time=datetime(2026, 9, 22, 11, 0),
        high=1790.0,
        low=1780.0,
        volume=100_000.0,
        now_ny=datetime(2026, 9, 22, 11, 20),
        market_open=True,
        day_high=1800.0,
        day_low=1750.0,
        session_date=TODAY,
        session_volume=2_000_000.0,
        session_bar_count=10,
    )
    kwargs.update(overrides)
    return assess_15m_bar(**kwargs)


def test_healthy_bar_passes() -> None:
    res = _assess()
    assert not res.is_stale and not res.is_anomalous and res.notes == []


def test_frozen_bar_is_flagged_stale() -> None:
    """SNDK 第 2 版：現價漲 5% 但 K 棒停在開盤第一根。"""
    res = _assess(
        bar_time=datetime(2026, 9, 22, 9, 30), now_ny=datetime(2026, 9, 22, 12, 40)
    )
    assert res.is_stale
    assert "延遲" in res.notes[0]


def test_staleness_not_checked_outside_market_hours() -> None:
    res = _assess(
        bar_time=datetime(2026, 9, 22, 15, 45),
        now_ny=datetime(2026, 9, 22, 20, 0),
        market_open=False,
    )
    assert not res.is_stale


def test_merged_bar_is_flagged() -> None:
    """SNDK 第 3 版：單根 15m K 棒吞下 226 萬股、開 1758 高 1895。"""
    res = _assess(
        high=1895.90,
        low=1758.14,
        volume=2_260_000.0,
        day_high=1895.90,
        day_low=1754.50,
        session_volume=3_000_000.0,
        session_bar_count=12,
    )
    assert res.is_anomalous
    assert any("合併" in n for n in res.notes)


def test_bar_volume_above_session_volume_is_flagged() -> None:
    res = _assess(volume=3_000_000.0, session_volume=2_000_000.0)
    assert res.is_anomalous


def test_bar_extreme_outside_day_range_is_flagged() -> None:
    """MU 第 1 版：15m 高 1050.29 > 全日高 1048.65。"""
    res = _assess(high=1050.29, low=1040.0, day_high=1048.65, day_low=1030.0)
    assert res.is_anomalous


def test_previous_day_bar_skips_range_checks() -> None:
    res = _assess(
        bar_time=datetime(2026, 9, 21, 15, 45),
        now_ny=datetime(2026, 9, 22, 9, 40),
        high=5000.0,
    )
    assert not res.is_anomalous


# ── 期權成交價無套利下界 ─────────────────────────────────────


def test_premium_below_intrinsic_falls_back_to_mid() -> None:
    """SNDK 第 1 版：現價 1789.42 時 $1650 CALL 權利金 136.05 < 內含 139.42。"""
    price = sanitize_option_trade_price(136.05, 140.0, 142.0, 1650.0, 1789.42, True)
    assert price == pytest.approx(141.0)


def test_premium_below_intrinsic_without_quote_is_dropped() -> None:
    assert sanitize_option_trade_price(136.05, 0.0, 0.0, 1650.0, 1789.42, True) is None


def test_valid_premium_is_kept() -> None:
    assert sanitize_option_trade_price(12.5, 12.0, 13.0, 1800.0, 1789.42, True) == 12.5
    # 價內 PUT：內含 10.58
    assert sanitize_option_trade_price(11.0, 10.8, 11.2, 1800.0, 1789.42, False) == 11.0


# ── Session 統計 ─────────────────────────────────────────────


@pytest.mark.asyncio
@patch("services.market_data_service.get_history_df", new_callable=AsyncMock)
async def test_fetch_session_stats_same_source(mock_hist: AsyncMock) -> None:
    idx = pd.DatetimeIndex([datetime(2026, 9, 22, 9, 30), datetime(2026, 9, 22, 9, 45)])
    mock_hist.return_value = pd.DataFrame(
        {
            "Open": [100.0, 101.0],
            "High": [102.0, 103.0],
            "Low": [98.0, 99.0],
            "Close": [100.0, 101.0],
            "Volume": [1000.0, 3000.0],
        },
        index=idx,
    )
    stats = await fetch_session_stats("AAPL")
    assert stats is not None
    assert stats.vwap == pytest.approx(100.75)
    assert (stats.high, stats.low, stats.volume, stats.bar_count) == (
        103.0,
        98.0,
        4000.0,
        2,
    )
    assert stats.session_date == TODAY
    assert stats.low <= stats.vwap <= stats.high


@pytest.mark.asyncio
@patch("services.market_data_service.get_history_df", new_callable=AsyncMock)
async def test_fetch_session_stats_empty_returns_none(mock_hist: AsyncMock) -> None:
    mock_hist.return_value = pd.DataFrame()
    assert await fetch_session_stats("AAPL") is None
