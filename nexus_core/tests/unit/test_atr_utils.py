"""
tests/unit/test_atr_utils.py

單元測試：market_analysis/atr_utils.py 的真實 15 分鐘 K 棒 ATR(14) 計算。
"""

from unittest.mock import AsyncMock, patch

import pandas as pd
import pytest

from market_analysis.atr_utils import fetch_atr_15m, fetch_high_60d


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


def _make_daily_df_with_spike(n: int = 65, spike_high: float = 500.0) -> pd.DataFrame:
    """建構日線測試資料：除最後一根 (今日) 外皆為遞增序列，今日為遠高於
    歷史區間的極端尖峰，用來驗證 shift(1) 確實排除當日高點——若未排除，
    「今日創新高」會把自己算進「前 60 日最高價」，等於用未來資料驗證
    突破成立（前視偏差）。"""
    highs = [100.0 + i for i in range(n - 1)] + [spike_high]
    lows = [h - 1.0 for h in highs]
    closes = [h - 0.5 for h in highs]
    return pd.DataFrame({"High": highs, "Low": lows, "Close": closes})


@pytest.mark.asyncio
@patch("services.market_data_service.get_history_df", new_callable=AsyncMock)
async def test_fetch_high_60d_happy_path_excludes_today(
    mock_get_history_df: AsyncMock,
) -> None:
    """正常路徑：回傳「前 60 個交易日」(不含當日) 的最高價，且必以
    period='1y'、interval='1d' 呼叫 get_history_df (與 fetch_atr_1d 同一份
    抓取慣例，不 force_refresh)。"""
    mock_get_history_df.return_value = _make_daily_df_with_spike(n=65)

    result = await fetch_high_60d("AAPL")

    mock_get_history_df.assert_awaited_once_with("AAPL", period="1y", interval="1d")
    # window 為 shift(1) 後的 60 根，即原始序列 index 4..63，最大值為 100+63=163
    assert result == pytest.approx(163.0)
    # 今日尖峰 (500) 必須被排除，否則代表 shift(1) 防前視失效
    assert result < 500.0


@pytest.mark.asyncio
@patch("services.market_data_service.get_history_df", new_callable=AsyncMock)
async def test_fetch_high_60d_returns_zero_when_insufficient_bars(
    mock_get_history_df: AsyncMock,
) -> None:
    """日線根數不足 61 根 (60 根 rolling window + 1 根 shift 位移) 時，
    fail-safe 回傳 0.0，不強行計算出可能失真的數值。"""
    mock_get_history_df.return_value = _make_daily_df_with_spike(n=60)

    result = await fetch_high_60d("AAPL")
    assert result == 0.0


@pytest.mark.asyncio
@patch("services.market_data_service.get_history_df", new_callable=AsyncMock)
async def test_fetch_high_60d_returns_zero_when_data_empty(
    mock_get_history_df: AsyncMock,
) -> None:
    """日線資料為空 DataFrame 時，fail-safe 回傳 0.0。"""
    mock_get_history_df.return_value = pd.DataFrame()

    result = await fetch_high_60d("AAPL")
    assert result == 0.0


@pytest.mark.asyncio
@patch("services.market_data_service.get_history_df", new_callable=AsyncMock)
async def test_fetch_high_60d_returns_zero_when_history_none(
    mock_get_history_df: AsyncMock,
) -> None:
    """get_history_df 回傳 None (例如標的無效) 時，fail-safe 回傳 0.0。"""
    mock_get_history_df.return_value = None

    result = await fetch_high_60d("AAPL")
    assert result == 0.0


@pytest.mark.asyncio
@patch("services.market_data_service.get_history_df", new_callable=AsyncMock)
async def test_fetch_high_60d_returns_zero_when_history_fetch_raises(
    mock_get_history_df: AsyncMock,
) -> None:
    """get_history_df 拋出例外 (網路/API 異常) 時，fail-safe 回傳 0.0，
    不得將例外向上傳播中斷呼叫端的並行 gather。"""
    mock_get_history_df.side_effect = Exception("mocked network failure")

    result = await fetch_high_60d("AAPL")
    assert result == 0.0
