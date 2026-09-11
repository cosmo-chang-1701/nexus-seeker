from typing import Any
from datetime import datetime, timedelta
import math
from unittest.mock import AsyncMock, patch
import pandas as pd
import pytest

from market_analysis.sentiment.options_flow import calculate_pcr
from market_analysis.sentiment.max_pain import _calculate_max_pain_with_weights
from market_analysis.sentiment.iv_metrics import _calculate_straddle_implied_em


@pytest.mark.asyncio
async def test_issue_2_6_index_etf_dynamic_pcr_thresholds_and_dte_30() -> None:
    """ISSUE-2.6: 大盤 ETF (SPY) 採用 DTE<=30 天匯總與動態 PCR 門檻 (1.15 仍判定為偏多/常態避險)"""
    calls_df = pd.DataFrame([{"volume": 100.0, "openInterest": 500.0}])
    puts_df = pd.DataFrame([{"volume": 115.0, "openInterest": 575.0}])  # PCR = 1.15

    class MockChain:
        def __init__(self) -> None:
            self.calls = calls_df
            self.puts = puts_df

    today = datetime.now().date()
    # 構造 10 個到期日 (含 0-DTE, 1-DTE, 7-DTE, 14-DTE, 28-DTE, 45-DTE)
    expiries = [
        (today + timedelta(days=d)).strftime("%Y-%m-%d")
        for d in [0, 1, 2, 5, 8, 15, 22, 29, 45, 60]
    ]

    requested_expiries: list[str] = []

    async def mock_get_chain(symbol: str, expiry: str, **kwargs: Any) -> MockChain:
        requested_expiries.append(expiry)
        return MockChain()

    # 1. 測試 SPY (大盤 ETF)
    with patch(
        "services.market_data_service.get_all_option_expiries",
        new_callable=AsyncMock,
        return_value=expiries,
    ), patch(
        "services.market_data_service.get_option_chain", side_effect=mock_get_chain
    ), patch(
        "market_analysis.sentiment.options_flow.save_sentiment_history",
        new_callable=AsyncMock,
    ):
        spy_res = await calculate_pcr("SPY")

    # SPY 應匯總 30 天內合約 (0, 1, 2, 5, 8, 15, 22, 29 共 8 個)，而非僅前 3 個
    assert len(requested_expiries) == 8
    assert spy_res["volume_pcr"] == 1.15
    # 大盤 ETF PCR <= 1.20 屬於常態避險，判定為看漲主導而非個股的看空主導
    assert "看漲主導" in spy_res["volume_pcr_state"]

    # 2. 測試普通個股 (AAPL)
    requested_expiries.clear()
    with patch(
        "services.market_data_service.get_all_option_expiries",
        new_callable=AsyncMock,
        return_value=expiries,
    ), patch(
        "services.market_data_service.get_option_chain", side_effect=mock_get_chain
    ), patch(
        "market_analysis.sentiment.options_flow.save_sentiment_history",
        new_callable=AsyncMock,
    ):
        aapl_res = await calculate_pcr("AAPL")

    # 個股僅取前 3 個到期日
    assert len(requested_expiries) == 3
    assert aapl_res["volume_pcr"] == 1.15
    # 個股 PCR 1.15 > 1.10 判定為偏向空頭
    assert "空頭" in aapl_res["volume_pcr_state"]


def test_iss_10_max_pain_prefix_sums_and_plateau_closest_to_spot() -> None:
    """ISS-10: Max Pain 前綴和算法計算正確，平原期選取距離現價最近之履約價 (消除向下偏差)"""
    # 構造對稱未平倉分佈：
    # Strikes: 90, 100, 110, 120
    # Calls: 90 (OI 100), 100 (OI 100)
    # Puts:  110 (OI 100), 120 (OI 100)
    # 痛點在 100 與 110 處完全相等 (最小值平原期)
    calls = pd.DataFrame(
        {
            "strike": [90.0, 100.0],
            "openInterest": [100.0, 100.0],
            "volume": [10.0, 10.0],
        }
    )
    puts = pd.DataFrame(
        {
            "strike": [110.0, 120.0],
            "openInterest": [100.0, 100.0],
            "volume": [10.0, 10.0],
        }
    )

    class MockChain:
        def __init__(self, c: pd.DataFrame, p: pd.DataFrame) -> None:
            self.calls = c
            self.puts = p

    chain = MockChain(calls, puts)

    # 現價接近 110 (如 spot = 108.0)
    # 舊演算法會因 pains.index(min(pains)) 永遠選最左邊的 100.0
    # 新演算法在平原期選取距離現價最近的 110.0
    mp_at_108 = _calculate_max_pain_with_weights(
        chain, weight_key="openInterest", spot_price=108.0
    )
    assert mp_at_108 == 110.0

    # 若現價接近 100 (如 spot = 101.0)
    mp_at_101 = _calculate_max_pain_with_weights(
        chain, weight_key="openInterest", spot_price=101.0
    )
    assert mp_at_101 == 100.0


@pytest.mark.asyncio
async def test_iss_11_straddle_expected_move_one_sigma_scaling() -> None:
    """ISS-11: ATM Straddle Expected Move 採用標準 1σ 比例 sqrt(pi/2) ≈ 1.2533 平移"""
    calls = pd.DataFrame([{"strike": 100.0, "bid": 2.9, "ask": 3.1, "lastPrice": 3.0}])
    puts = pd.DataFrame([{"strike": 100.0, "bid": 2.9, "ask": 3.1, "lastPrice": 3.0}])

    class MockChain:
        def __init__(self) -> None:
            self.calls = calls
            self.puts = puts

    # DTE = 7 天 (剛好 1 週)
    today = datetime.now().date()
    exp_7d = (today + timedelta(days=7)).strftime("%Y-%m-%d")

    with patch(
        "services.market_data_service.get_all_option_expiries",
        new_callable=AsyncMock,
        return_value=[exp_7d],
    ), patch(
        "services.market_data_service.get_option_chain",
        new_callable=AsyncMock,
        return_value=MockChain(),
    ):
        em = await _calculate_straddle_implied_em("TEST", 100.0)

    # Straddle = 3.0 + 3.0 = 6.0
    # 1σ EM = 6.0 * sqrt(pi / 2) * sqrt(7 / 7) ≈ 6.0 * 1.253314 ≈ 7.52
    expected = 6.0 * math.sqrt(math.pi / 2.0)
    assert em == pytest.approx(expected, rel=1e-3)
