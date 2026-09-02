"""單元測試：反向ETF標的解析與現貨動能確認
(market_analysis/dynamic_rollover/inverse_hedge.py)。

僅涵蓋 Scenario 4 (保證金防禦) 第三轉倉目的地的獨立輔助函式；
evaluate_margin_defense_impl 整合層測試見 test_dynamic_rollover.py。
"""

from unittest.mock import AsyncMock, patch

import pandas as pd
import pytest

from market_analysis.dynamic_rollover.inverse_hedge import (
    confirm_inverse_hedge_spot_momentum,
    get_inverse_symbol,
    select_inverse_leverage_tier,
)


def test_select_inverse_leverage_tier_double_confirmation_uses_2x() -> None:
    assert select_inverse_leverage_tier(True, True) == "2x"


def test_select_inverse_leverage_tier_single_confirmation_uses_1x() -> None:
    assert select_inverse_leverage_tier(True, False) == "1x"
    assert select_inverse_leverage_tier(False, True) == "1x"
    assert select_inverse_leverage_tier(False, False) == "1x"


def test_get_inverse_symbol_single_stock_prefers_requested_tier() -> None:
    assert get_inverse_symbol("NVDA", "1x") == "NVDD"
    assert get_inverse_symbol("NVDA", "2x") == "NVD"


def test_get_inverse_symbol_single_stock_falls_back_when_tier_missing() -> None:
    # AAPL 僅收錄 1x 商品，要求 2x 時應退回 1x 而非回傳 None
    assert get_inverse_symbol("AAPL", "2x") == "AAPD"
    # AAOI 僅收錄 2x 商品，要求 1x 時應退回 2x
    assert get_inverse_symbol("AAOI", "1x") == "AAOZ"


def test_get_inverse_symbol_index_direct_mapping() -> None:
    assert get_inverse_symbol("QQQ") == "SQQQ"
    assert get_inverse_symbol("SPY") == "SH"


def test_get_inverse_symbol_falls_back_to_sector_inverse() -> None:
    # MU 未收錄於 SINGLE_STOCK_INVERSE_MAP，依 risk_engine.SECTOR_BENCHMARK_MAP
    # 分類為 SMH -> 回退至產業反向ETF SOXS
    assert get_inverse_symbol("MU") == "SOXS"


def test_get_inverse_symbol_unknown_symbol_falls_back_to_spy_inverse() -> None:
    # 完全未知的標的 (risk_engine.get_sector_benchmark 預設回傳 SPY，
    # 且 SPY 本身亦不在 SECTOR_INVERSE_MAP 中) -> 最終回退至大盤反向ETF SH
    assert get_inverse_symbol("ZZZZ_UNKNOWN") == "SH"


@pytest.mark.asyncio
async def test_confirm_inverse_hedge_spot_momentum_fails_closed_on_fetch_error() -> (
    None
):
    with patch(
        "services.market_data_service.get_history_df",
        new_callable=AsyncMock,
        side_effect=Exception("network error"),
    ):
        assert await confirm_inverse_hedge_spot_momentum("SQQQ") is False


@pytest.mark.asyncio
async def test_confirm_inverse_hedge_spot_momentum_fails_closed_on_insufficient_bars() -> (
    None
):
    tiny_df = pd.DataFrame(
        {
            "Open": [1.0],
            "High": [1.0],
            "Low": [1.0],
            "Close": [1.0],
            "Volume": [100.0],
        }
    )
    with patch(
        "services.market_data_service.get_history_df",
        new_callable=AsyncMock,
        return_value=tiny_df,
    ):
        assert await confirm_inverse_hedge_spot_momentum("SQQQ") is False


@pytest.mark.asyncio
async def test_confirm_inverse_hedge_spot_momentum_true_when_all_gates_pass() -> None:
    # 建構一段持續上漲、成交額充足的合成日K，確保 RSI14>50、收盤>MA10、
    # 平均成交額 >= 最低門檻，三項確認皆通過。
    n = 40
    closes = [100.0 + i * 1.5 for i in range(n)]
    df = pd.DataFrame(
        {
            "Open": closes,
            "High": [c * 1.01 for c in closes],
            "Low": [c * 0.99 for c in closes],
            "Close": closes,
            "Volume": [1_000_000.0] * n,
        }
    )
    with patch(
        "services.market_data_service.get_history_df",
        new_callable=AsyncMock,
        return_value=df,
    ):
        assert await confirm_inverse_hedge_spot_momentum("SQQQ") is True


@pytest.mark.asyncio
async def test_confirm_inverse_hedge_spot_momentum_false_when_illiquid() -> None:
    # 同樣的上漲走勢，但成交量過低 (未達最低日均成交額門檻) -> 應回傳 False，
    # 避免推薦流動性過薄的反向ETF。
    n = 40
    closes = [100.0 + i * 1.5 for i in range(n)]
    df = pd.DataFrame(
        {
            "Open": closes,
            "High": [c * 1.01 for c in closes],
            "Low": [c * 0.99 for c in closes],
            "Close": closes,
            "Volume": [10.0] * n,
        }
    )
    with patch(
        "services.market_data_service.get_history_df",
        new_callable=AsyncMock,
        return_value=df,
    ):
        assert await confirm_inverse_hedge_spot_momentum("SQQQ") is False


@pytest.mark.asyncio
async def test_confirm_inverse_hedge_spot_momentum_false_when_downtrend() -> None:
    # 走勢下跌 (RSI14 應 < 50 且收盤 < MA10) -> 反向ETF自身未出現買入動能 -> False
    n = 40
    closes = [100.0 - i * 1.5 for i in range(n)]
    df = pd.DataFrame(
        {
            "Open": closes,
            "High": [c * 1.01 for c in closes],
            "Low": [c * 0.99 for c in closes],
            "Close": closes,
            "Volume": [1_000_000.0] * n,
        }
    )
    with patch(
        "services.market_data_service.get_history_df",
        new_callable=AsyncMock,
        return_value=df,
    ):
        assert await confirm_inverse_hedge_spot_momentum("SQQQ") is False
