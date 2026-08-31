"""
tests/unit/test_atr_utils.py

單元測試：market_analysis/atr_utils.py 的真實 15 分鐘 K 棒 ATR(14) 計算。
"""

from unittest.mock import AsyncMock, patch

import pandas as pd
import pytest

from market_analysis.atr_utils import fetch_atr_15m


def _make_ohlc_df(n: int, base: float = 100.0, step: float = 0.5) -> pd.DataFrame:
    """建構一組具真實波動 (非固定值) 的 OHLC 測試資料，確保 pandas_ta.atr
    能算出非零、非 NaN 的結果。"""
    highs = [base + i * step + 1.0 for i in range(n)]
    lows = [base + i * step - 1.0 for i in range(n)]
    closes = [base + i * step for i in range(n)]
    return pd.DataFrame({"High": highs, "Low": lows, "Close": closes})


@pytest.mark.asyncio
@patch("services.market_data_service.get_history_df", new_callable=AsyncMock)
async def test_fetch_atr_15m_happy_path(mock_get_history_df: AsyncMock) -> None:
    """正常路徑：15m K 棒資料充足時，回傳真實計算出的 ATR(14) 數值 (非 0.0)，
    且必以 force_refresh=True、interval='15m'、period='5d' 呼叫 get_history_df。"""
    mock_get_history_df.return_value = _make_ohlc_df(30)

    result = await fetch_atr_15m("AAPL")

    mock_get_history_df.assert_awaited_once_with(
        "AAPL", period="5d", interval="15m", force_refresh=True
    )
    assert result > 0.0


@pytest.mark.asyncio
@patch("services.market_data_service.get_history_df", new_callable=AsyncMock)
async def test_fetch_atr_15m_respects_force_refresh_override(
    mock_get_history_df: AsyncMock,
) -> None:
    """force_refresh 參數必須原樣透傳給 get_history_df，供呼叫端 (例如低頻率
    背景排程) 選擇性關閉繞過快取。"""
    mock_get_history_df.return_value = _make_ohlc_df(30)

    await fetch_atr_15m("AAPL", force_refresh=False)

    mock_get_history_df.assert_awaited_once_with(
        "AAPL", period="5d", interval="15m", force_refresh=False
    )


@pytest.mark.asyncio
@patch("services.market_data_service.get_history_df", new_callable=AsyncMock)
async def test_fetch_atr_15m_returns_zero_when_data_empty(
    mock_get_history_df: AsyncMock,
) -> None:
    """15m K 棒資料為空 DataFrame 時，fail-safe 回傳 0.0。"""
    mock_get_history_df.return_value = pd.DataFrame()

    result = await fetch_atr_15m("AAPL")
    assert result == 0.0


@pytest.mark.asyncio
@patch("services.market_data_service.get_history_df", new_callable=AsyncMock)
async def test_fetch_atr_15m_returns_zero_when_insufficient_bars(
    mock_get_history_df: AsyncMock,
) -> None:
    """15m K 棒數量不足 ATR(14) 所需的最小根數時，fail-safe 回傳 0.0，
    不強行計算出可能失真的數值。"""
    mock_get_history_df.return_value = _make_ohlc_df(5)

    result = await fetch_atr_15m("AAPL")
    assert result == 0.0


@pytest.mark.asyncio
@patch("services.market_data_service.get_history_df", new_callable=AsyncMock)
async def test_fetch_atr_15m_returns_zero_when_history_fetch_raises(
    mock_get_history_df: AsyncMock,
) -> None:
    """get_history_df 拋出例外 (網路/API 異常) 時，fail-safe 回傳 0.0，
    不得將例外向上傳播中斷呼叫端的並行 gather。"""
    mock_get_history_df.side_effect = Exception("mocked network failure")

    result = await fetch_atr_15m("AAPL")
    assert result == 0.0


@pytest.mark.asyncio
@patch("services.market_data_service.get_history_df", new_callable=AsyncMock)
async def test_fetch_atr_15m_returns_zero_when_history_none(
    mock_get_history_df: AsyncMock,
) -> None:
    """get_history_df 回傳 None (例如標的無效) 時，fail-safe 回傳 0.0。"""
    mock_get_history_df.return_value = None

    result = await fetch_atr_15m("AAPL")
    assert result == 0.0
