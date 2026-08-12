"""負 Gamma 懸崖確認引擎的單元測試。"""

import pytest
from unittest.mock import AsyncMock, patch

import pandas as pd

from market_analysis.gamma_cliff_confirmation import (
    is_gamma_cliff_confirmed,
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
