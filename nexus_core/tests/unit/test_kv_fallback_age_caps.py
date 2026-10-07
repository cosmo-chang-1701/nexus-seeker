"""PR-C：kv 快取 fallback 讀取的年齡上限。

涵蓋 get_kv_cache_fresh 邊界、Vol POC／GEX PutWall 24h 上限、
macro_vix 30 分鐘回退上限、FedWatch 12h 上限（逾期時評分函式收到 prob=None）。
"""

import logging
from typing import Any, Optional
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from database.cache import get_kv_cache_fresh
from market_analysis.intraday_pipeline.metrics import (
    get_cached_gex_putwall,
    get_cached_volume_poc,
)

_AGE = "database.cache.get_kv_cache_with_age"


# ---------- get_kv_cache_fresh 邊界 ----------


@pytest.mark.parametrize(
    "value, age, expected",
    [
        (1.5, 100.0, 1.5),  # 新鮮
        (1.5, 600.0, 1.5),  # 剛好等於上限仍可用
        (1.5, 600.1, None),  # 超過上限
        (1.5, None, None),  # 年齡未知一律不可用
        (None, None, None),  # 查無資料
        (0.0, 10.0, 0.0),  # 假值（0.0）不得被誤判為缺值
    ],
)
def test_get_kv_cache_fresh_boundaries(
    value: Optional[float], age: Optional[float], expected: Optional[float]
) -> None:
    with patch(_AGE, return_value=(value, age)):
        assert get_kv_cache_fresh("k", 600.0) == expected


# ---------- Vol POC / PutWall：24h ----------


@pytest.mark.parametrize(
    "getter, key",
    [
        (get_cached_volume_poc, "volume_poc_AAPL"),
        (get_cached_gex_putwall, "gex_putwall_AAPL"),
    ],
)
def test_level_fallback_24h_cap(getter: Any, key: str) -> None:
    with patch(_AGE, return_value=(150.0, 25 * 3600)) as m:
        assert getter("aapl") is None
        m.assert_called_with(key)
    with patch(_AGE, return_value=(150.0, 23 * 3600)):
        assert getter("aapl") == 150.0
    with patch(_AGE, return_value=(150.0, None)):
        assert getter("aapl") is None
    with patch(_AGE, return_value=(None, None)):
        assert getter("aapl") is None


# ---------- macro_vix：30 分鐘 ----------


@pytest.fixture
def mock_bot() -> Any:
    bot = MagicMock()
    bot._is_leader_instance = True
    bot.queue_dm = AsyncMock()
    bot.wait_until_ready = AsyncMock()
    bot.get_cog = MagicMock(return_value=None)
    return bot


async def _run_scanner_with_bad_vix(mock_bot: Any, cached: tuple[Any, Any]) -> None:
    from cogs.trading.scheduler import SchedulerCog

    with patch("market_time.is_market_open", return_value=True), patch(
        "services.llm_service.is_memory_safe", return_value=True
    ), patch("services.market_data_service.get_quote") as mock_quote, patch(
        "services.market_data_service.get_vix_term_structure"
    ) as mock_vts, patch(
        "market_analysis.index_microstructure.fetch_core_macro_metrics",
        new_callable=AsyncMock,
    ), patch(
        "cogs.trading.scheduler._sync_edge_watchlist", new_callable=AsyncMock
    ), patch("database.get_all_watchlist", return_value=[]), patch(
        "database.get_all_user_ids", return_value=[]
    ), patch("database.is_notification_enabled", return_value=True), patch(
        "database.get_kv_cache", return_value=None
    ), patch("database.save_kv_cache", new_callable=AsyncMock), patch(
        _AGE, return_value=cached
    ):
        mock_quote.side_effect = (
            lambda sym: {"c": 0.0} if sym == "^VIX" else {"c": 5000.0}
        )
        mock_vts.return_value = {
            "vts_ratio": 0.0,
            "vts_state": "UNKNOWN",
            "is_valid": False,
        }
        cog = SchedulerCog(mock_bot)
        await cog.dynamic_market_scanner()
        cog.intraday_pipeline.stop()
        cog.dynamic_market_scanner.cancel()
        cog.daily_reddit_update.cancel()


@pytest.mark.asyncio
async def test_vix_fallback_rejected_when_older_than_30min(
    mock_bot: Any, caplog: pytest.LogCaptureFixture
) -> None:
    with caplog.at_level(logging.INFO):
        await _run_scanner_with_bad_vix(mock_bot, (18.0, 31 * 60))
    assert "VIX 快取回退" not in caplog.text


@pytest.mark.asyncio
async def test_vix_fallback_used_when_within_30min(
    mock_bot: Any, caplog: pytest.LogCaptureFixture
) -> None:
    with caplog.at_level(logging.INFO):
        await _run_scanner_with_bad_vix(mock_bot, (18.0, 29 * 60))
    assert "VIX 快取回退" in caplog.text


# ---------- FedWatch：12h，逾期 -> prob=None ----------


@pytest.mark.asyncio
async def test_fedwatch_stale_passes_none_to_scorer() -> None:
    from market_analysis.squeeze_entry.vetoes import compute_macro_escape_tier

    for age, expected_prob in ((13 * 3600, None), (11 * 3600, 0.8)):
        with patch(_AGE, return_value=(0.8, age)), patch(
            "services.market_data_service.get_vix_term_structure",
            new_callable=AsyncMock,
            return_value={"is_valid": True, "vts_ratio": 0.9},
        ), patch(
            "market_analysis.index_microstructure.fetch_core_macro_metrics",
            new_callable=AsyncMock,
            return_value={"fear_greed": 40.0},
        ), patch(
            "market_analysis.index_microstructure.get_market_regime",
            new_callable=AsyncMock,
            return_value="NORMAL",
        ), patch(
            "market_analysis.index_microstructure.evaluate_macro_top_escape_score",
            return_value=(0, "NORMAL", "", []),
        ) as scorer:
            await compute_macro_escape_tier()
            assert scorer.call_args.kwargs["prob"] == expected_prob


def test_scorer_treats_none_prob_as_unknown_without_scoring() -> None:
    """確認 prob=None 不計分（門檻不變）；其餘已知因子皆正常時 tier=UNKNOWN 而非 NORMAL。"""
    from market_analysis.index_microstructure import evaluate_macro_top_escape_score

    score, tier, _, _ = evaluate_macro_top_escape_score(
        vts_ratio=0.9,
        fear_greed=40.0,
        prob=None,
        is_negative_gamma=False,
        satellite_euphoria_ratio=None,
    )
    assert score == 0
    assert tier == "UNKNOWN"
