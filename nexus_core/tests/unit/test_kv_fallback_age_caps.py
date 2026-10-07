"""PR-C：kv 快取 fallback 讀取的年齡上限。

涵蓋 get_kv_cache_fresh 邊界、Vol POC／GEX PutWall 以交易日為準的有效性
（含週末）、macro_vix 40 分鐘回退上限與不自我刷新、FedWatch 12h 上限
（逾期時評分函式收到 prob=None 且 prob_stale=True）、雷達 POC 不繞過上限。
"""

import logging
from datetime import datetime
from zoneinfo import ZoneInfo
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


# ---------- Vol POC / PutWall：以交易日為準 ----------

_ET = ZoneInfo("America/New_York")


def _et(y: int, m: int, d: int, h: int, mi: int = 0) -> datetime:
    return datetime(y, m, d, h, mi, tzinfo=_ET)


def _age(written: datetime, now: datetime) -> float:
    return (now - written).total_seconds()


# 2026-10-05 為週一；10-02 週五、10-01 週四、09-30 週三
@pytest.mark.parametrize(
    "written, now, expected",
    [
        # 週五盤中寫入、週一盤中讀取：有效（跨週末，> 48h）
        (_et(2026, 10, 2, 11), _et(2026, 10, 5, 10), True),
        # 週五收盤後寫入、週一盤前讀取：有效
        (_et(2026, 10, 2, 17), _et(2026, 10, 5, 8), True),
        # 週三寫入、週五盤中讀取：無效（上一個已收盤交易日為週四）
        (_et(2026, 9, 30, 11), _et(2026, 10, 2, 11), False),
        # 週四寫入、週五盤中讀取：有效
        (_et(2026, 10, 1, 11), _et(2026, 10, 2, 11), True),
        # 週四寫入、週一讀取：無效（上一個已收盤交易日為週五）
        (_et(2026, 10, 1, 15), _et(2026, 10, 5, 10), False),
        # 當日剛寫入：有效
        (_et(2026, 10, 5, 10), _et(2026, 10, 5, 10, 30), True),
    ],
)
def test_trading_day_validity(written: datetime, now: datetime, expected: bool) -> None:
    import market_time

    assert (
        market_time.is_cache_age_within_last_session(_age(written, now), as_of=now)
        is expected
    )


def test_trading_day_validity_unknown_age_is_invalid() -> None:
    import market_time

    assert market_time.is_cache_age_within_last_session(None) is False


@pytest.mark.parametrize(
    "getter, key",
    [
        (get_cached_volume_poc, "volume_poc_AAPL"),
        (get_cached_gex_putwall, "gex_putwall_AAPL"),
    ],
)
def test_level_fallback_uses_trading_day_window(getter: Any, key: str) -> None:
    now = _et(2026, 10, 5, 10)  # 週一
    fri_age = _age(_et(2026, 10, 2, 11), now)
    wed_age = _age(_et(2026, 9, 30, 11), now)
    with patch("market_time.datetime") as mdt:
        mdt.now.return_value = now
        with patch(_AGE, return_value=(150.0, fri_age)) as m:
            assert getter("aapl") == 150.0
            m.assert_called_with(key)
        with patch(_AGE, return_value=(150.0, wed_age)):
            assert getter("aapl") is None
        with patch(_AGE, return_value=(150.0, None)):
            assert getter("aapl") is None
        with patch(_AGE, return_value=(None, None)):
            assert getter("aapl") is None


# ---------- macro_vix：40 分鐘 ----------


@pytest.fixture
def mock_bot() -> Any:
    bot = MagicMock()
    bot._is_leader_instance = True
    bot.queue_dm = AsyncMock()
    bot.wait_until_ready = AsyncMock()
    bot.get_cog = MagicMock(return_value=None)
    return bot


async def _run_scanner_with_bad_vix(
    mock_bot: Any, cached: tuple[Any, Any]
) -> AsyncMock:
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
    ), patch("database.save_kv_cache", new_callable=AsyncMock) as mock_save, patch(
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
    return mock_save


@pytest.mark.asyncio
async def test_vix_fallback_rejected_when_older_than_40min(
    mock_bot: Any, caplog: pytest.LogCaptureFixture
) -> None:
    with caplog.at_level(logging.INFO):
        await _run_scanner_with_bad_vix(mock_bot, (18.0, 41 * 60))
    assert "VIX 快取回退" not in caplog.text


@pytest.mark.asyncio
async def test_vix_fallback_used_when_within_40min_and_not_resaved(
    mock_bot: Any, caplog: pytest.LogCaptureFixture
) -> None:
    """35 分鐘仍可回退（30 分鐘舊上限會拒絕），且回退值不得被存回刷新時間戳。"""
    with caplog.at_level(logging.INFO):
        mock_save = await _run_scanner_with_bad_vix(mock_bot, (18.0, 35 * 60))
    assert "VIX 快取回退" in caplog.text
    saved_keys = [c.args[0] for c in mock_save.call_args_list]
    assert "macro_vix" not in saved_keys


@pytest.mark.asyncio
async def test_vix_live_quote_is_saved(mock_bot: Any) -> None:
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
    ), patch("database.save_kv_cache", new_callable=AsyncMock) as mock_save:
        mock_quote.side_effect = lambda sym: (
            {"c": 20.0} if sym == "^VIX" else {"c": 5000.0}
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
    assert ("macro_vix", 20.0) in [c.args for c in mock_save.call_args_list]


# ---------- FedWatch：12h，逾期 -> prob=None ----------


@pytest.mark.parametrize(
    "value, age, expected",
    [
        (0.8, 11 * 3600, (0.8, False)),
        (0.8, 13 * 3600, (None, True)),  # 有值但逾期
        (0.8, None, (None, True)),  # 年齡未知視為過期
        (None, None, (None, False)),  # 查無資料不是過期
    ],
)
def test_get_fedwatch_probability_fresh(
    value: Any, age: Any, expected: tuple[Any, bool]
) -> None:
    from database.cache import get_fedwatch_probability_fresh

    with patch(_AGE, return_value=(value, age)):
        assert get_fedwatch_probability_fresh() == expected


@pytest.mark.asyncio
async def test_fedwatch_stale_passes_none_and_stale_flag_to_scorer(
    caplog: pytest.LogCaptureFixture,
) -> None:
    from market_analysis.squeeze_entry.vetoes import compute_macro_escape_tier

    for age, expected_prob, expected_stale in (
        (13 * 3600, None, True),
        (11 * 3600, 0.8, False),
    ):
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
        ) as scorer, caplog.at_level(logging.WARNING):
            await compute_macro_escape_tier()
            assert scorer.call_args.kwargs["prob"] == expected_prob
            assert scorer.call_args.kwargs["prob_stale"] is expected_stale
    assert "FedWatch 資料過期" in caplog.text


def test_scorer_stale_label_and_fail_closed() -> None:
    from market_analysis.index_microstructure import evaluate_macro_top_escape_score

    kwargs: dict[str, Any] = dict(
        vts_ratio=0.9,
        fear_greed=40.0,
        prob=None,
        is_negative_gamma=False,
        satellite_euphoria_ratio=None,
    )
    score, tier, _, factors = evaluate_macro_top_escape_score(**kwargs, prob_stale=True)
    assert (score, tier) == (0, "UNKNOWN")  # 仍 fail-closed，門檻不變
    label = dict(factors)["FOMC 鷹派傾向分數 (FedWatch)"]
    assert "FedWatch 資料過期（逾 12 小時），不計分" in label
    _, _, _, factors_default = evaluate_macro_top_escape_score(**kwargs)
    assert "資料不足" in dict(factors_default)["FOMC 鷹派傾向分數 (FedWatch)"]


def test_calendar_service_ignores_stale_kv_fedwatch() -> None:
    """09:00 簡報路徑與進場閘門共用 12h 上限：逾期快取不得被當成有效值。"""
    from services.calendar_service import calendar_service

    def _kv(key: str) -> Any:
        return 0 if key == "macro_fedwatch_is_fallback" else None

    with patch("database.cache.get_kv_cache", side_effect=_kv), patch(
        "database.cache.get_fedwatch_probability_fresh", return_value=(None, True)
    ), patch("services.calendar_service.get_read_connection") as conn:
        conn.return_value.cursor.return_value.fetchone.return_value = None
        prob, is_fallback = calendar_service.get_latest_fedwatch_probability()
    assert prob == 0.50
    assert is_fallback is True


# ---------- /x 雷達：POC 不繞過上限、不存回備援值 ----------


def test_radar_kv_snapshot_session_fresh() -> None:
    from cogs.unified_terminal.radar_data import _KvSnapshot

    now = _et(2026, 10, 5, 10)
    snap = _KvSnapshot(
        {
            "volume_poc_FRI": (100.0, _age(_et(2026, 10, 2, 11), now)),
            "volume_poc_WED": (100.0, _age(_et(2026, 9, 30, 11), now)),
        }
    )
    with patch("market_time.datetime") as mdt:
        mdt.now.return_value = now
        assert snap.get_session_fresh("volume_poc_FRI") == 100.0
        assert snap.get_session_fresh("volume_poc_WED") is None
        assert snap.get_session_fresh("volume_poc_NONE") is None


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


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "poc_age, expected_poc", [(60.0, 123.0), (30 * 24 * 3600.0, 0.0)]
)
async def test_radar_slow_poc_fallback_respects_cap_and_not_resaved(
    poc_age: float, expected_poc: float
) -> None:
    """vp_data 無 hvn 時：備援 POC 僅在有效期內採用；任何情況都不得把備援值存回。"""
    import pandas as pd

    from cogs.unified_terminal import UnifiedTerminalCog

    cog = UnifiedTerminalCog(MagicMock())
    fake_df = pd.DataFrame(
        {"Close": [100.0] * 30, "High": [105.0] * 30, "Low": [95.0] * 30}
    )
    saved: dict[str, Any] = {}

    async def fake_save_kv(k: str, v: Any) -> None:
        saved[k] = v

    def fake_age(key: str) -> tuple[Any, Any]:
        if key.startswith("volume_poc_"):
            return 123.0, poc_age
        return None, 0.0

    with patch(
        "services.market_data_service.get_quote",
        return_value={"c": 100.0, "volume": 1000},
    ), patch(
        "services.market_data_service.get_history_df", return_value=fake_df
    ), patch(
        "database.squeeze_cache.get_squeeze_cache",
        return_value={"momentum": 2.5, "direction": "🟢", "is_squeezing": True},
    ), patch(
        "market_analysis.index_microstructure.fetch_symbol_gex_metrics",
        return_value={},
    ), patch(
        "market_analysis.sentiment_engine.SentimentEngine.detect_uoa_with_physical_caps",
        return_value=([], []),
    ), patch(
        "market_analysis.sentiment_engine.SentimentEngine.calculate_pcr",
        return_value={},
    ), patch(
        "market_analysis.sentiment_engine.SentimentEngine.calculate_skew",
        return_value={"skew": 0.5, "skew_percentile": 90.0},
    ), patch(
        "market_analysis.sentiment_engine.SentimentEngine.fetch_and_calculate_iv_metrics",
        return_value=None,
    ), patch(
        "market_analysis.sentiment_engine.SentimentEngine.get_expected_move",
        return_value={},
    ), patch(
        "market_analysis.volume_profile.calculate_volume_profile_from_df",
        return_value={},
    ), patch(
        "market_analysis.volume_profile.calculate_volume_profile", return_value={}
    ), patch("database.cache.save_kv_cache", side_effect=fake_save_kv), patch(
        "database.cache.get_kv_cache_with_age", side_effect=fake_age
    ), patch(
        "market_analysis.sentiment_engine.SentimentEngine.get_unified_max_pain",
        return_value={"max_pain": 100.0},
    ), patch("market_time.is_cache_age_within_last_session") as mock_valid:
        mock_valid.side_effect = lambda age, as_of=None: (
            age is not None and age < 24 * 3600
        )
        result = await cog._fetch_sym_radar_data_slow_raw("NVDA")
    assert result["volume_poc"] == expected_poc
    assert "volume_poc_NVDA" not in saved
