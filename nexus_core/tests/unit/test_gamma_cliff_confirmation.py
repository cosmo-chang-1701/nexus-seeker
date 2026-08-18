"""負 Gamma 懸崖確認引擎的單元測試。"""

import pytest
from unittest.mock import AsyncMock, patch

import pandas as pd

from market_analysis.gamma_cliff_confirmation import (
    is_gamma_cliff_confirmed,
    is_below_gamma_defense_line,
    MIN_CONFIRMATION_WINDOW,
)


def _make_candle_df(closes: list[float], n: int = 0) -> pd.DataFrame:
    """建構模擬的 1 分鐘 K 線 DataFrame。"""
    if not n:
        n = len(closes)
    return pd.DataFrame(
        {
            "Open": [c + 0.5 for c in closes],
            "High": [c + 1.0 for c in closes],
            "Low": [c - 0.5 for c in closes],
            "Close": closes,
            "Volume": [1000] * n,
        }
    )


@pytest.mark.asyncio
@patch("services.market_data_service.get_history_df", new_callable=AsyncMock)
async def test_all_candles_below_cliff_confirms_breakdown(
    mock_get_history: AsyncMock,
) -> None:
    """所有 15 根 K 線收盤價均低於懸崖線 → 確認結構性破位"""
    df = _make_candle_df([214.0] * 15)
    mock_get_history.return_value = df

    result = await is_gamma_cliff_confirmed("TEST", 215.0)
    assert result is True


@pytest.mark.asyncio
@patch("services.market_data_service.get_history_df", new_callable=AsyncMock)
async def test_some_candles_above_cliff_rejects(mock_get_history: AsyncMock) -> None:
    """部分 K 線收盤價在懸崖線上方 → 判定為雜訊，不觸發清倉"""
    closes = [214.0] * 10 + [216.0] * 5
    df = _make_candle_df(closes)
    mock_get_history.return_value = df

    result = await is_gamma_cliff_confirmed("TEST", 215.0)
    assert result is False


@pytest.mark.asyncio
@patch("services.market_data_service.get_history_df", new_callable=AsyncMock)
async def test_insufficient_data_returns_false(mock_get_history: AsyncMock) -> None:
    """K 線數據不足 → 保守返回 False"""
    df = _make_candle_df([214.0] * 5)
    mock_get_history.return_value = df

    result = await is_gamma_cliff_confirmed("TEST", 215.0)
    assert result is False


@pytest.mark.asyncio
@patch("services.market_data_service.get_history_df", new_callable=AsyncMock)
async def test_api_error_returns_false(mock_get_history: AsyncMock) -> None:
    """API 異常 → 保守返回 False（不觸發清倉）"""
    mock_get_history.side_effect = Exception("API Error")

    result = await is_gamma_cliff_confirmed("TEST", 215.0)
    assert result is False


@pytest.mark.asyncio
@patch("services.market_data_service.get_history_df", new_callable=AsyncMock)
async def test_custom_window_length(mock_get_history: AsyncMock) -> None:
    """自訂確認窗口長度 (10 分鐘) 搭配 10 根 K 線均低於懸崖 → 確認"""
    df = _make_candle_df([214.0] * 10)
    mock_get_history.return_value = df

    result = await is_gamma_cliff_confirmed(
        "TEST", 215.0, confirmation_window_minutes=10
    )
    assert result is True


@pytest.mark.asyncio
async def test_zero_cliff_level_returns_false() -> None:
    """懸崖價位為 0 → 無法確認，返回 False"""
    result = await is_gamma_cliff_confirmed("TEST", 0.0)
    assert result is False


@pytest.mark.asyncio
@patch("services.market_data_service.get_history_df", new_callable=AsyncMock)
async def test_window_clamping_to_minimum(mock_get_history: AsyncMock) -> None:
    """窗口值低於下限 (3) → 被鉗制至 MIN_CONFIRMATION_WINDOW (5)"""
    df = _make_candle_df([214.0] * MIN_CONFIRMATION_WINDOW)
    mock_get_history.return_value = df

    # 傳入 3 應被鉗制至 5
    result = await is_gamma_cliff_confirmed(
        "TEST", 215.0, confirmation_window_minutes=3
    )
    assert result is True


@pytest.mark.asyncio
@patch("services.market_data_service.get_history_df", new_callable=AsyncMock)
async def test_empty_dataframe_returns_false(mock_get_history: AsyncMock) -> None:
    """空 DataFrame → 保守返回 False"""
    mock_get_history.return_value = pd.DataFrame()

    result = await is_gamma_cliff_confirmed("TEST", 215.0)
    assert result is False


# ---------------------------------------------------------------------------
# #11: is_below_gamma_defense_line — scenario_classifier 共用的粗粒度基礎判定
# ---------------------------------------------------------------------------


def test_is_below_gamma_defense_line_true_when_below_both() -> None:
    assert (
        is_below_gamma_defense_line(price=200.0, put_wall=210.0, gamma_flip=215.0)
        is True
    )


def test_is_below_gamma_defense_line_false_when_above_put_wall() -> None:
    assert (
        is_below_gamma_defense_line(price=212.0, put_wall=210.0, gamma_flip=215.0)
        is False
    )


def test_is_below_gamma_defense_line_false_when_above_gamma_flip() -> None:
    assert (
        is_below_gamma_defense_line(price=216.0, put_wall=210.0, gamma_flip=215.0)
        is False
    )


def test_is_below_gamma_defense_line_false_when_walls_missing() -> None:
    assert (
        is_below_gamma_defense_line(price=100.0, put_wall=0.0, gamma_flip=115.0)
        is False
    )
    assert (
        is_below_gamma_defense_line(price=100.0, put_wall=110.0, gamma_flip=0.0)
        is False
    )


@pytest.mark.asyncio
@patch("services.market_data_service.get_history_df", new_callable=AsyncMock)
async def test_deliberate_divergence_heartbeat_vs_dynamic_rollover_gamma_cliff_level(
    mock_get_history: AsyncMock,
) -> None:
    """
    #11 刻意記錄的三方分歧：cogs/trading/heartbeat.py 的
    gamma_cliff_level = min(put_wall, gamma_flip)（無 ATR 緩衝）與
    market_analysis/dynamic_rollover.py 的
    gamma_cliff_level = anchor_base - 1.5*atr_14（含 ATR 緩衝）刻意不同，
    不應合併為單一公式——這是設計意圖而非缺陷。

    put_wall=210, gamma_flip=215, atr_14=2.0：
      - heartbeat.py 版本：min(210, 215) = 210 (無緩衝)
      - dynamic_rollover.py 版本：210 - 1.5*2.0 = 207 (含緩衝，更嚴謹)

    同一份 1 分鐘 K 線資料 (連續收在 $208)：
      - 貫穿 heartbeat.py 的門檻 ($208 < $210) → watchlist 心跳確認結構性破位
      - 未貫穿 dynamic_rollover.py 的門檻 ($208 >= $207) → 持倉轉倉不確認破位，維持 HOLD
    """
    df = _make_candle_df([208.0] * 15)
    mock_get_history.return_value = df

    heartbeat_style_level = min(210.0, 215.0)  # cogs/trading/heartbeat.py 公式
    dynamic_rollover_style_level = 210.0 - 1.5 * 2.0  # dynamic_rollover.py 公式

    heartbeat_confirmed = await is_gamma_cliff_confirmed("TEST", heartbeat_style_level)
    rollover_confirmed = await is_gamma_cliff_confirmed(
        "TEST", dynamic_rollover_style_level
    )

    assert heartbeat_confirmed is True
    assert rollover_confirmed is False
