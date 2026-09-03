"""Unit tests for the 15m Microstructure (15分鐘微觀結構) fields in 標的分析中心."""

from datetime import datetime, timedelta
from typing import Any, Dict
from unittest.mock import AsyncMock, patch

import pandas as pd
import pytest

import market_time
from cogs.embed_builders.portfolio_embeds import create_tactical_symbol_embed
from market_analysis.price_volume_alert import (
    Confirmed15mBar,
    get_confirmed_15m_bar,
)


def _get_embed_text(embed: Any) -> str:
    parts: list[str] = [str(embed.description or "")]
    for field in getattr(embed, "fields", []):
        parts.append(str(field.name))
        parts.append(str(field.value))
    return "\n".join(parts)


def test_15m_microstructure_exact_prompt_example() -> None:
    """測試與需求規範範例完全吻合之 15m 微觀結構數據呈現：
    開 378.10 | 高 380.46 | 低 377.90 | 收 378.87 (實體陽線)
    15m 成交量: 1,420,500 股
    15m 均量 (SMA20): 850,000 股
    即時量比 (RVOL_15m): 1.67x (狀態: 🟢 放量突破 >= 1.5x)
    """
    bar = Confirmed15mBar(
        symbol="QQQ",
        bar_time=datetime.now(),
        open=378.10,
        high=380.46,
        low=377.90,
        close=378.87,
        volume=1420500.0,
        avg_volume=850000.0,
    )
    data: Dict[str, Any] = {
        "symbol": "QQQ",
        "bar_15m": bar,
    }

    embed = create_tactical_symbol_embed(data)
    text = _get_embed_text(embed)

    # 驗證欄位標題
    assert "⏱️ 15分鐘微觀結構 (15m Microstructure)" in text

    # 驗證四個指標項目內容
    assert (
        "最新 15m K棒: 開 378.10 | 高 380.46 | 低 377.90 | 收 378.87 (實體陽線)" in text
    )
    assert "15m 成交量: 1,420,500 股" in text
    assert "15m 均量 (SMA20): 850,000 股" in text
    assert "即時量比 (RVOL_15m): 1.67x (狀態: 🟢 放量突破 >= 1.5x)" in text


def test_15m_microstructure_bearish_candle_and_low_rvol() -> None:
    """測試實體陰線 (Close < Open) 與未達標量比 (< 1.5x, ❌ 缺乏放量代償)。"""
    bar = Confirmed15mBar(
        symbol="SPY",
        bar_time=datetime.now(),
        open=550.00,
        high=552.00,
        low=545.00,
        close=547.50,
        volume=800000.0,
        avg_volume=1000000.0,
    )
    data: Dict[str, Any] = {
        "symbol": "SPY",
        "bar_15m": bar,
    }

    embed = create_tactical_symbol_embed(data)
    text = _get_embed_text(embed)

    assert (
        "最新 15m K棒: 開 550.00 | 高 552.00 | 低 545.00 | 收 547.50 (實體陰線)" in text
    )
    assert "15m 成交量: 800,000 股" in text
    assert "15m 均量 (SMA20): 1,000,000 股" in text
    assert "即時量比 (RVOL_15m): 0.80x (狀態: ❌ 缺乏放量代償 < 1.5x)" in text


def test_15m_microstructure_flat_doji_candle() -> None:
    """測試平盤十字 (Close == Open)。"""
    data: Dict[str, Any] = {
        "symbol": "NVDA",
        "open_15m": 125.00,
        "high_15m": 126.00,
        "low_15m": 124.50,
        "close_15m": 125.00,
        "volume_15m": 1500000.0,
        "volume_15m_sma20": 1000000.0,
    }

    embed = create_tactical_symbol_embed(data)
    text = _get_embed_text(embed)

    assert (
        "最新 15m K棒: 開 125.00 | 高 126.00 | 低 124.50 | 收 125.00 (平盤十字)" in text
    )
    assert "15m 成交量: 1,500,000 股" in text
    assert "15m 均量 (SMA20): 1,000,000 股" in text
    assert "即時量比 (RVOL_15m): 1.50x (狀態: 🟢 放量突破 >= 1.5x)" in text


def test_15m_microstructure_dict_input() -> None:
    """測試以 dict 格式傳入 bar_15m。"""
    data: Dict[str, Any] = {
        "symbol": "TSLA",
        "bar_15m": {
            "open": 210.50,
            "high": 212.00,
            "low": 210.00,
            "close": 211.80,
            "volume": 2000000.0,
            "avg_volume": 1200000.0,
        },
    }

    embed = create_tactical_symbol_embed(data)
    text = _get_embed_text(embed)

    assert (
        "最新 15m K棒: 開 210.50 | 高 212.00 | 低 210.00 | 收 211.80 (實體陽線)" in text
    )
    assert "15m 成交量: 2,000,000 股" in text
    assert "15m 均量 (SMA20): 1,200,000 股" in text
    assert "即時量比 (RVOL_15m): 1.67x (狀態: 🟢 放量突破 >= 1.5x)" in text


def test_15m_microstructure_degraded_when_bar_none() -> None:
    """測試當 bar_15m 為 None（數據缺失/盤前未更新）時的優雅降級處理。"""
    data: Dict[str, Any] = {
        "symbol": "AAPL",
        "bar_15m": None,
    }

    embed = create_tactical_symbol_embed(data)
    text = _get_embed_text(embed)

    assert "⏱️ 15分鐘微觀結構 (15m Microstructure)" in text
    assert "最新 15m K棒: -- (暫無數據 / 待開盤)" in text
    assert "15m 成交量: -- 股" in text
    assert "15m 均量 (SMA20): -- 股" in text
    assert "即時量比 (RVOL_15m): -- (狀態: ⚠️ 數據源缺失)" in text


def test_15m_microstructure_omitted_when_no_15m_in_data() -> None:
    """測試當 data 完全沒有任何 15m 相關鍵值時，不破壞舊有調用端。"""
    data: Dict[str, Any] = {
        "symbol": "AAPL",
    }

    embed = create_tactical_symbol_embed(data)
    text = _get_embed_text(embed)

    assert "⏱️ 15分鐘微觀結構 (15m Microstructure)" not in text


@pytest.mark.asyncio
async def test_get_confirmed_15m_bar_populates_ohlc() -> None:
    """測試 get_confirmed_15m_bar 確實填入 open, high, low, close 數值。"""
    now_naive = datetime.now(market_time.ny_tz).replace(tzinfo=None)
    last_bar_start = now_naive - timedelta(minutes=20)
    idx = [last_bar_start - timedelta(minutes=15 * i) for i in range(25)][::-1]

    opens = [100.0] * 24 + [378.10]
    highs = [101.0] * 24 + [380.46]
    lows = [99.0] * 24 + [377.90]
    closes = [100.5] * 24 + [378.87]
    volumes = [500.0] * 24 + [1420500.0]

    df = pd.DataFrame(
        {
            "Open": opens,
            "High": highs,
            "Low": lows,
            "Close": closes,
            "Volume": volumes,
        },
        index=pd.DatetimeIndex(idx),
    )

    with patch(
        "services.market_data_service.get_history_df", new_callable=AsyncMock
    ) as mock_hist:
        mock_hist.return_value = df
        bar = await get_confirmed_15m_bar("QQQ")

    assert bar is not None
    assert bar.open == 378.10
    assert bar.high == 380.46
    assert bar.low == 377.90
    assert bar.close == 378.87
    assert bar.volume == 1420500.0
    assert bar.avg_volume == 500.0


def test_15m_microstructure_zero_or_nan_prices_handled_safely() -> None:
    """測試當 Open/Close 為 0.0 或 NaN 時，正確顯示數據不全，而非誤判為平盤十字或輸出 nan。"""

    data: Dict[str, Any] = {
        "symbol": "TSLA",
        "bar_15m": {
            "open": 0.0,
            "high": float("nan"),
            "low": 0.0,
            "close": 200.0,
            "volume": 50000.0,
            "avg_volume": 40000.0,
        },
    }
    embed = create_tactical_symbol_embed(data)
    text = _get_embed_text(embed)

    assert "最新 15m K棒: -- (數據不全)" in text
    assert "nan" not in text.lower()


def test_15m_microstructure_zero_sma20_yields_missing_status() -> None:
    """測試當均量 SMA20 為 0 或無基準時，即時量比應優雅降級為缺失狀態，避免除以零或誤報。"""
    data: Dict[str, Any] = {
        "symbol": "COIN",
        "open_15m": 250.0,
        "high_15m": 255.0,
        "low_15m": 249.0,
        "close_15m": 252.0,
        "volume_15m": 100000.0,
        "volume_15m_sma20": 0.0,
    }
    embed = create_tactical_symbol_embed(data)
    text = _get_embed_text(embed)

    assert "即時量比 (RVOL_15m): -- (狀態: ⚠️ 數據源缺失)" in text


def test_confirmed_15m_bar_none_ohlc_defaults() -> None:
    """測試 Confirmed15mBar 預設 open, high, low 為 None，向後相容舊有建構語法。"""
    bar = Confirmed15mBar(
        symbol="AMD",
        bar_time=datetime.now(),
        close=150.0,
        volume=10000.0,
        avg_volume=8000.0,
    )
    assert bar.open is None
    assert bar.high is None
    assert bar.low is None
