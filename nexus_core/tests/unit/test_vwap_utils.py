"""
tests/unit/test_vwap_utils.py

單元測試：market_analysis/vwap_utils.py 的當前交易時段 Session VWAP 計算。
"""

from unittest.mock import AsyncMock, patch

import pandas as pd
import pytest

from market_analysis.vwap_utils import fetch_session_vwap


def _make_session_df() -> pd.DataFrame:
    """建構單一交易時段的 15m OHLCV 測試資料，手算 VWAP 供驗證。"""
    return pd.DataFrame(
        {
            "Open": [100.0, 101.0],
            "High": [102.0, 103.0],
            "Low": [98.0, 99.0],
            "Close": [100.0, 101.0],
            "Volume": [1000.0, 3000.0],
        }
    )


@pytest.mark.asyncio
@patch("services.market_data_service.get_history_df", new_callable=AsyncMock)
async def test_fetch_session_vwap_happy_path(mock_get_history_df: AsyncMock) -> None:
    """正常路徑：手算 typical_price*volume 累加 / 累加volume，且必以
    force_refresh=True、interval='15m'、period='1d' 呼叫 get_history_df。"""
    mock_get_history_df.return_value = _make_session_df()

    result = await fetch_session_vwap("AAPL")

    mock_get_history_df.assert_awaited_once_with(
        "AAPL", period="1d", interval="15m", force_refresh=True
    )
    # typical_price bar1 = (102+98+100)/3 = 100.0, bar2 = (103+99+101)/3 = 101.0
    # vwap = (100*1000 + 101*3000) / 4000 = 100.75
    assert result == pytest.approx(100.75)


@pytest.mark.asyncio
@patch("services.market_data_service.get_history_df", new_callable=AsyncMock)
async def test_fetch_session_vwap_respects_force_refresh_override(
    mock_get_history_df: AsyncMock,
) -> None:
    """force_refresh 參數必須原樣透傳給 get_history_df。"""
    mock_get_history_df.return_value = _make_session_df()

    await fetch_session_vwap("AAPL", force_refresh=False)

    mock_get_history_df.assert_awaited_once_with(
        "AAPL", period="1d", interval="15m", force_refresh=False
    )


@pytest.mark.asyncio
@patch("services.market_data_service.get_history_df", new_callable=AsyncMock)
async def test_fetch_session_vwap_returns_zero_when_data_empty(
    mock_get_history_df: AsyncMock,
) -> None:
    """15m K 棒資料為空 DataFrame 時，fail-safe 回傳 0.0。"""
    mock_get_history_df.return_value = pd.DataFrame()

    result = await fetch_session_vwap("AAPL")
    assert result == 0.0


@pytest.mark.asyncio
@patch("services.market_data_service.get_history_df", new_callable=AsyncMock)
async def test_fetch_session_vwap_returns_zero_when_volume_is_zero(
    mock_get_history_df: AsyncMock,
) -> None:
    """全數成交量為 0（如盤前佔位K棒）時，fail-safe 回傳 0.0，避免除以零。"""
    mock_get_history_df.return_value = pd.DataFrame(
        {
            "Open": [100.0],
            "High": [100.0],
            "Low": [100.0],
            "Close": [100.0],
            "Volume": [0.0],
        }
    )

    result = await fetch_session_vwap("AAPL")
    assert result == 0.0


@pytest.mark.asyncio
@patch("services.market_data_service.get_history_df", new_callable=AsyncMock)
async def test_fetch_session_vwap_returns_zero_when_history_fetch_raises(
    mock_get_history_df: AsyncMock,
) -> None:
    """get_history_df 拋出例外（網路/API 異常）時，fail-safe 回傳 0.0，
    不得將例外向上傳播中斷呼叫端的並行 gather。"""
    mock_get_history_df.side_effect = Exception("mocked network failure")

    result = await fetch_session_vwap("AAPL")
    assert result == 0.0


@pytest.mark.asyncio
@patch("services.market_data_service.get_history_df", new_callable=AsyncMock)
async def test_fetch_session_vwap_returns_zero_when_history_none(
    mock_get_history_df: AsyncMock,
) -> None:
    """get_history_df 回傳 None（例如標的無效）時，fail-safe 回傳 0.0。"""
    mock_get_history_df.return_value = None

    result = await fetch_session_vwap("AAPL")
    assert result == 0.0
