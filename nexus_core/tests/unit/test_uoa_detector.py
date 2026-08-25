from typing import Any

import pandas as pd
import pytest
from unittest.mock import patch

from market_analysis.sentiment.uoa_detector import (
    detect_uoa,
    detect_uoa_with_physical_caps,
)


class MockChain:
    def __init__(self, calls: Any, puts: Any):
        self.calls = calls
        self.puts = puts


def _make_chain() -> MockChain:
    calls_df = pd.DataFrame(
        [
            {
                # 物理封頂候選：ratio=0.9 (>=0.8), volume=900 (>=500), STO (trade_price==bid)
                "strike": 110.0,
                "volume": 900.0,
                "openInterest": 1000.0,
                "lastPrice": 1.00,
                "bid": 1.00,
                "ask": 1.10,
                "impliedVolatility": 0.3,
            },
            {
                # ratio 未達標 (0.4 < 0.8)，應排除
                "strike": 115.0,
                "volume": 400.0,
                "openInterest": 1000.0,
                "lastPrice": 1.00,
                "bid": 1.00,
                "ask": 1.10,
                "impliedVolatility": 0.3,
            },
            {
                # ratio 達標但方向為 BTO (trade_price==ask)，應排除
                "strike": 120.0,
                "volume": 900.0,
                "openInterest": 1000.0,
                "lastPrice": 1.10,
                "bid": 1.00,
                "ask": 1.10,
                "impliedVolatility": 0.3,
            },
        ]
    )
    puts_df = pd.DataFrame([])
    return MockChain(calls_df, puts_df)


@pytest.mark.asyncio
async def test_detect_uoa_with_physical_caps_filters_ratio_and_direction() -> None:
    """驗證全鏈 STO 物理封頂掃描：僅 ratio>=0.8x 且 volume>=500 且分類為 STO(Bid) 的履約價存活。"""
    expiries = ["2026-08-28"]
    quote = {"c": 100.0}
    mock_chain = _make_chain()

    with patch(
        "services.market_data_service.get_all_option_expiries", return_value=expiries
    ), patch(
        "services.market_data_service.get_option_chain", return_value=mock_chain
    ), patch("services.market_data_service.get_quote", return_value=quote):
        top5, physical_caps = await detect_uoa_with_physical_caps("TEST")

    assert len(physical_caps) == 1
    assert physical_caps[0]["strike"] == 110.0
    assert physical_caps[0]["type"] == "CALL"
    assert physical_caps[0]["action"] == "STO"
    assert physical_caps[0]["ratio"] == pytest.approx(0.9)


@pytest.mark.asyncio
async def test_detect_uoa_with_physical_caps_matches_detect_uoa_top5() -> None:
    """驗證新函式與 detect_uoa() 共用同一次期權鏈抓取後，回傳的前 5 大 UOA 清單一致。"""
    expiries = ["2026-08-28"]
    quote = {"c": 100.0}

    with patch(
        "services.market_data_service.get_all_option_expiries", return_value=expiries
    ), patch(
        "services.market_data_service.get_option_chain", return_value=_make_chain()
    ), patch("services.market_data_service.get_quote", return_value=quote):
        legacy_top5 = await detect_uoa("TEST")

    with patch(
        "services.market_data_service.get_all_option_expiries", return_value=expiries
    ), patch(
        "services.market_data_service.get_option_chain", return_value=_make_chain()
    ), patch("services.market_data_service.get_quote", return_value=quote):
        new_top5, _ = await detect_uoa_with_physical_caps("TEST")

    assert [item["strike"] for item in legacy_top5] == [
        item["strike"] for item in new_top5
    ]


@pytest.mark.asyncio
async def test_detect_uoa_output_has_delta_and_dte_fields() -> None:
    """驗證 detect_uoa() 輸出 dict 帶有 delta/dte 欄位 (Item 3：UOA 意圖映射重構)。"""
    expiries = ["2026-08-28"]
    quote = {"c": 100.0}

    calls_df = pd.DataFrame(
        [
            {
                "strike": 100.0,
                "volume": 2000.0,
                "openInterest": 100.0,
                "lastPrice": 5.0,
                "bid": 4.9,
                "ask": 5.1,
                "impliedVolatility": 0.35,
            }
        ]
    )
    mock_chain = MockChain(calls_df, pd.DataFrame([]))

    with patch(
        "services.market_data_service.get_all_option_expiries", return_value=expiries
    ), patch(
        "services.market_data_service.get_option_chain", return_value=mock_chain
    ), patch("services.market_data_service.get_quote", return_value=quote):
        results = await detect_uoa("TEST")

    assert len(results) == 1
    assert "delta" in results[0]
    assert "dte" in results[0]
    assert isinstance(results[0]["dte"], int)
