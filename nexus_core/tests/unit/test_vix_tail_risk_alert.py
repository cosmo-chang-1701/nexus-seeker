from typing import Any, Dict
import pytest
from unittest.mock import AsyncMock, MagicMock, patch
from datetime import datetime, timezone

from cogs.embed_builders.alert_embeds import create_vix_tail_risk_embed
from cogs.trading.scheduler import SchedulerCog


@pytest.fixture
def mock_bot() -> Any:
    bot = MagicMock()
    bot._is_leader_instance = True
    bot.queue_dm = AsyncMock()
    bot.wait_until_ready = AsyncMock()
    bot.get_cog = MagicMock(return_value=None)
    return bot


def test_create_vix_tail_risk_embed_formatting() -> None:
    """Test create_vix_tail_risk_embed handles valid and invalid inputs gracefully."""
    # 1. Valid panic inputs
    embed = create_vix_tail_risk_embed(
        vts_ratio=1.15,
        vix=32.5,
        trigger_reason="VIX 飆升至 32.5 (突破 30.0 極端恐慌線)",
    )
    assert embed.title == "🦇 雷達：VIX 期限結構倒掛與黑天鵝預警"
    fields: Dict[str, str] = {
        str(f.name): str(f.value)
        for f in embed.fields
        if f.name is not None and f.value is not None
    }
    assert "`1.15` (嚴重倒掛 🚨)" in fields["📐 VIX 期限結構比 (VTS)"]
    assert "`32.5` (極端恐慌 🚨)" in fields["🌐 目前 VIX"]
    assert "VIX 飆升至 32.5" in fields["🎯 觸發原因"]

    # 2. Defensive fallback on 0.0 or invalid inputs
    embed_invalid = create_vix_tail_risk_embed(vts_ratio=0.0, vix=0.0)
    fields_invalid: Dict[str, str] = {
        str(f.name): str(f.value)
        for f in embed_invalid.fields
        if f.name is not None and f.value is not None
    }
    assert "`N/A` (數據未更新)" in fields_invalid["📐 VIX 期限結構比 (VTS)"]
    assert "`N/A` (數據異常)" in fields_invalid["🌐 目前 VIX"]


@pytest.mark.asyncio
async def test_dynamic_market_scanner_blocks_false_alarm_vix_zero(
    mock_bot: Any,
) -> None:
    """
    [User Bug Reproduction Case]
    When VIX=0.0 (API failure) and VTS=1.0 (empty fallback),
    the system MUST NOT trigger any tail risk alert.
    """
    with patch("market_time.is_market_open", return_value=True), patch(
        "services.market_data_service.get_quote"
    ) as mock_quote, patch(
        "services.market_data_service.get_vix_term_structure"
    ) as mock_vts, patch(
        "market_analysis.dark_pool_engine.fetch_and_cache_darkpool_dix",
        new_callable=AsyncMock,
    ), patch(
        "market_analysis.index_microstructure.fetch_core_macro_metrics",
        new_callable=AsyncMock,
    ), patch(
        "cogs.trading.heartbeat.dispatch_watchlist_heartbeat",
        new_callable=AsyncMock,
    ), patch("database.get_all_watchlist", return_value=[]), patch(
        "database.get_all_user_ids", return_value=[123456789]
    ), patch("database.is_notification_enabled", return_value=True), patch(
        "database.get_kv_cache", return_value=None
    ), patch("database.save_kv_cache", new_callable=AsyncMock):
        # Mock API outputs: VIX is 0.0 (empty), VTS is invalid
        mock_quote.side_effect = (
            lambda sym: {"c": 0.0} if sym == "^VIX" else {"c": 5000.0}
        )
        mock_vts.return_value = {
            "vts_ratio": 0.0,
            "vts_state": "UNKNOWN",
            "is_valid": False,
            "vix_front": None,
            "vix_back": None,
        }

        cog = SchedulerCog(mock_bot)
        await cog.dynamic_market_scanner()

        # Ensure NO DM was queued
        mock_bot.queue_dm.assert_not_called()
        cog.intraday_pipeline.stop()
        cog.dynamic_market_scanner.cancel()
        cog.daily_reddit_update.cancel()


@pytest.mark.asyncio
async def test_dynamic_market_scanner_blocks_false_alarm_mild_vts(
    mock_bot: Any,
) -> None:
    """
    When VIX=15.0 and VTS=1.00 (flat),
    it is NOT a black swan event, so no alert should be sent.
    """
    with patch("market_time.is_market_open", return_value=True), patch(
        "services.market_data_service.get_quote"
    ) as mock_quote, patch(
        "services.market_data_service.get_vix_term_structure"
    ) as mock_vts, patch(
        "market_analysis.dark_pool_engine.fetch_and_cache_darkpool_dix",
        new_callable=AsyncMock,
    ), patch(
        "market_analysis.index_microstructure.fetch_core_macro_metrics",
        new_callable=AsyncMock,
    ), patch(
        "cogs.trading.heartbeat.dispatch_watchlist_heartbeat",
        new_callable=AsyncMock,
    ), patch("database.get_all_watchlist", return_value=[]), patch(
        "database.get_all_user_ids", return_value=[123456789]
    ), patch("database.is_notification_enabled", return_value=True), patch(
        "database.get_kv_cache", return_value=None
    ), patch("database.save_kv_cache", new_callable=AsyncMock):
        mock_quote.side_effect = (
            lambda sym: {"c": 15.0} if sym == "^VIX" else {"c": 5000.0}
        )
        mock_vts.return_value = {
            "vts_ratio": 1.00,
            "vts_state": "Backwardation",
            "is_valid": True,
            "vix_front": 15.0,
            "vix_back": 15.0,
        }

        cog = SchedulerCog(mock_bot)
        await cog.dynamic_market_scanner()

        # Ensure NO DM was queued
        mock_bot.queue_dm.assert_not_called()
        cog.intraday_pipeline.stop()
        cog.dynamic_market_scanner.cancel()
        cog.daily_reddit_update.cancel()


@pytest.mark.asyncio
async def test_dynamic_market_scanner_triggers_on_vix_surge(mock_bot: Any) -> None:
    """
    When VIX >= 30.0 (valid panic), it triggers the tail-risk alert
    and writes to KV cache cooldown.
    """
    saved_kv: Dict[str, Any] = {}

    async def mock_save(k: str, v: Any) -> None:
        saved_kv[k] = v

    def mock_get(k: str) -> Any:
        return saved_kv.get(k)

    with patch("market_time.is_market_open", return_value=True), patch(
        "services.market_data_service.get_quote"
    ) as mock_quote, patch(
        "services.market_data_service.get_vix_term_structure"
    ) as mock_vts, patch(
        "market_analysis.dark_pool_engine.fetch_and_cache_darkpool_dix",
        new_callable=AsyncMock,
    ), patch(
        "market_analysis.index_microstructure.fetch_core_macro_metrics",
        new_callable=AsyncMock,
    ), patch(
        "cogs.trading.heartbeat.dispatch_watchlist_heartbeat",
        new_callable=AsyncMock,
    ), patch("database.get_all_watchlist", return_value=[]), patch(
        "database.get_all_user_ids", return_value=[123456789]
    ), patch("database.is_notification_enabled", return_value=True), patch(
        "database.get_kv_cache", side_effect=mock_get
    ), patch("database.save_kv_cache", side_effect=mock_save):
        mock_quote.side_effect = (
            lambda sym: {"c": 32.5} if sym == "^VIX" else {"c": 5000.0}
        )
        mock_vts.return_value = {
            "vts_ratio": 1.02,
            "vts_state": "Backwardation",
            "is_valid": True,
            "vix_front": 32.5,
            "vix_back": 31.8,
        }

        cog = SchedulerCog(mock_bot)
        await cog.dynamic_market_scanner()

        # Assert DM was queued once
        assert mock_bot.queue_dm.call_count == 1
        args, kwargs = mock_bot.queue_dm.call_args
        assert args[0] == 123456789
        embed = kwargs["embed"]
        assert embed.title == "🦇 雷達：VIX 期限結構倒掛與黑天鵝預警"

        # Check cooldown key was recorded
        today_str = datetime.now(timezone.utc).strftime("%Y%m%d")
        cooldown_key = f"macro_tail_risk_alert_123456789_{today_str}"
        assert saved_kv.get(cooldown_key) == 1

        # Second execution on same day: should NOT spam DM again due to cooldown
        mock_bot.queue_dm.reset_mock()
        await cog.dynamic_market_scanner()
        mock_bot.queue_dm.assert_not_called()

        cog.intraday_pipeline.stop()
        cog.dynamic_market_scanner.cancel()
        cog.daily_reddit_update.cancel()


@pytest.mark.asyncio
async def test_dynamic_market_scanner_triggers_on_severe_vts_inversion(
    mock_bot: Any,
) -> None:
    """
    When VTS >= 1.10 AND VIX >= 20.0 (severe backwardation with elevated fear),
    it triggers the tail-risk alert.
    """
    with patch("market_time.is_market_open", return_value=True), patch(
        "services.market_data_service.get_quote"
    ) as mock_quote, patch(
        "services.market_data_service.get_vix_term_structure"
    ) as mock_vts, patch(
        "market_analysis.dark_pool_engine.fetch_and_cache_darkpool_dix",
        new_callable=AsyncMock,
    ), patch(
        "market_analysis.index_microstructure.fetch_core_macro_metrics",
        new_callable=AsyncMock,
    ), patch(
        "cogs.trading.heartbeat.dispatch_watchlist_heartbeat",
        new_callable=AsyncMock,
    ), patch("database.get_all_watchlist", return_value=[]), patch(
        "database.get_all_user_ids", return_value=[987654321]
    ), patch("database.is_notification_enabled", return_value=True), patch(
        "database.get_kv_cache", return_value=None
    ), patch("database.save_kv_cache", new_callable=AsyncMock):
        mock_quote.side_effect = (
            lambda sym: {"c": 24.0} if sym == "^VIX" else {"c": 5000.0}
        )
        mock_vts.return_value = {
            "vts_ratio": 1.15,
            "vts_state": "Backwardation",
            "is_valid": True,
            "vix_front": 24.0,
            "vix_back": 20.87,
        }

        cog = SchedulerCog(mock_bot)
        await cog.dynamic_market_scanner()

        assert mock_bot.queue_dm.call_count == 1
        args, kwargs = mock_bot.queue_dm.call_args
        assert args[0] == 987654321

        cog.intraday_pipeline.stop()
        cog.dynamic_market_scanner.cancel()
        cog.daily_reddit_update.cancel()
