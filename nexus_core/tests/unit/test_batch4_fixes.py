"""Unit tests for Batch 4 fixes:
- [ISSUE-2.7]: Skew zero-axis deadband [-0.5%, +0.5%] suppressing microstructure noise.
- [ISS-13]: Embed rendering exposes IV Percentile side by side with IV Rank.
- [ISS-14]: Ultra-low volatility IV conflict threshold relaxed to 1.0% (0.01) for short-term Treasury ETFs.
"""

from unittest.mock import AsyncMock, MagicMock, patch
import pytest

from market_analysis.sentiment.skew_taxonomy import (
    classify_skew_state,
    SKEW_STATE_FLAT,
    SKEW_STATE_LEFT,
    SKEW_STATE_RIGHT,
    SKEW_STATE_DEFENSIVE,
    SKEW_STATE_BULLISH,
)
from market_analysis.sentiment.iv_metrics import fetch_and_calculate_iv_metrics


def test_classify_skew_state_deadband() -> None:
    """ISSUE-2.7: 驗證 [-0.5%, +0.5%] 零軸死區抑制微觀噪聲。"""
    # 樣本不足（冷啟動）時測試死區
    assert classify_skew_state(0.0, None) == SKEW_STATE_FLAT
    assert classify_skew_state(0.2, None) == SKEW_STATE_FLAT
    assert classify_skew_state(-0.3, None) == SKEW_STATE_FLAT
    assert classify_skew_state(0.5, None) == SKEW_STATE_FLAT
    assert classify_skew_state(-0.5, None) == SKEW_STATE_FLAT

    # 超過死區門檻
    assert classify_skew_state(0.51, None) == SKEW_STATE_LEFT
    assert classify_skew_state(1.5, None) == SKEW_STATE_LEFT
    assert classify_skew_state(-0.51, None) == SKEW_STATE_RIGHT
    assert classify_skew_state(-2.0, None) == SKEW_STATE_RIGHT

    # 有百分位時正常觸發極端態
    assert classify_skew_state(2.0, 95.0) == SKEW_STATE_DEFENSIVE
    assert classify_skew_state(-2.0, 10.0) == SKEW_STATE_BULLISH


@pytest.mark.asyncio
async def test_low_volatility_iv_conflict_threshold_relaxed() -> None:
    """ISS-14: 驗證超低波標的（如短債 ETF BIL/SHY，IV 落在 1%~5%）不被 Rule 4 誤殺。"""
    # 模擬短債標的：IV = 0.02 (2.0%)，IV Rank = 80.0%
    # 在過去 < 0.05 會觸發衝突退級，現在門檻放寬至 0.01，2.0% 應正常通過 LIVE_IV
    mock_conn1 = MagicMock()
    mock_cursor1 = MagicMock()
    mock_cursor1.fetchall.return_value = [
        (f"2026-01-{i:03d}", iv)
        for i, iv in enumerate([0.012, 0.015, 0.018, 0.020, 0.022] * 15)
    ]
    mock_conn1.cursor.return_value = mock_cursor1

    with patch(
        "services.market_data_service.get_quote",
        new=AsyncMock(return_value={"c": 91.50, "h": 91.60, "l": 91.40}),
    ), patch(
        "market_analysis.sentiment.iv_metrics.is_market_open",
        return_value=True,
    ), patch(
        "services.market_data_service.call_yf",
        new=AsyncMock(return_value={"impliedVolatility": 0.02}),
    ), patch(
        "database.connection.get_read_connection",
        return_value=mock_conn1,
    ), patch(
        "market_analysis.sentiment.iv_metrics._calculate_straddle_implied_em",
        new=AsyncMock(return_value=0.25),
    ), patch(
        "market_analysis.sentiment.iv_metrics._calculate_iv_term_structure",
        new=AsyncMock(return_value=("Contango", 0.95)),
    ):
        res = await fetch_and_calculate_iv_metrics("BIL", force_refresh=True)
        assert res.iv_source == "LIVE_IV"
        assert res.current_iv == pytest.approx(0.02, abs=0.001)
        assert res.iv_rank is not None and res.iv_rank > 70.0
        assert res.iv_rank <= 100.0

    # 驗證真異常（IV < 0.01 且 IV Rank > 70%）依然會被 Rule 4 衝突檢核攔截並退級
    mock_conn2 = MagicMock()
    mock_cursor2 = MagicMock()
    mock_cursor2.fetchall.return_value = [
        (f"2026-01-{i:03d}", iv)
        for i, iv in enumerate([0.001, 0.002, 0.003, 0.005, 0.006] * 15)
    ]
    mock_conn2.cursor.return_value = mock_cursor2

    with patch(
        "services.market_data_service.get_quote",
        new=AsyncMock(return_value={"c": 91.50, "h": 91.60, "l": 91.40}),
    ), patch(
        "market_analysis.sentiment.iv_metrics.is_market_open",
        return_value=True,
    ), patch(
        "services.market_data_service.call_yf",
        new=AsyncMock(return_value={"impliedVolatility": 0.005}),
    ), patch(
        "database.connection.get_read_connection",
        return_value=mock_conn2,
    ), patch(
        "market_analysis.sentiment.iv_metrics._calculate_straddle_implied_em",
        new=AsyncMock(return_value=0.05),
    ), patch(
        "market_analysis.sentiment.iv_metrics._calculate_iv_term_structure",
        new=AsyncMock(return_value=("Contango", 0.95)),
    ):
        res_conflict = await fetch_and_calculate_iv_metrics("BIL", force_refresh=True)
        assert res_conflict.iv_source == "UNAVAILABLE"
        assert res_conflict.iv_rank is None
