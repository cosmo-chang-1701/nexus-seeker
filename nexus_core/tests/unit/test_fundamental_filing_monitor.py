"""Unit tests for cogs.trading.fundamental_filing_monitor — the automated
daily scanner that detects new SEC filings (10-K/10-Q/8-K) for holding-only
symbols and routes them through the existing form-type-aware
DynamicRolloverEngine.evaluate_fundamental_thesis LLM pipeline.

Covers: no-new-filing skip, is_broken=True dispatch (respecting each
holder's notification toggle), is_broken=False silent path, memory-safety/
LLM-failure leaves the dedup cursor untouched, and one LLM call per unique
symbol even when multiple users hold it."""

from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from cogs.trading.fundamental_filing_monitor import FundamentalFilingMonitorCog
from market_analysis.dynamic_rollover import FundamentalThesisResult


@pytest.fixture
def mock_bot() -> Any:
    bot = MagicMock()
    bot._is_leader_instance = True
    bot.queue_dm = AsyncMock()
    bot.wait_until_ready = AsyncMock()
    return bot


def _holdings(*pairs: tuple[int, str]) -> list[dict[str, Any]]:
    return [{"user_id": uid, "symbol": sym} for uid, sym in pairs]


@pytest.mark.asyncio
async def test_scan_one_symbol_skips_when_no_new_filing(mock_bot: Any) -> None:
    cog = FundamentalFilingMonitorCog(mock_bot)

    with (
        patch(
            "services.fundamental_service.get_fundamental_reports_list",
            new_callable=AsyncMock,
            return_value=[{"accession_number": "0001-22", "form": "10-Q"}],
        ),
        patch(
            "database.market_cache.get_fundamental_scan_state",
            return_value={"last_accession_number": "0001-22", "last_form_type": "10-Q"},
        ),
        patch(
            "services.fundamental_service.get_fundamental_context",
            new_callable=AsyncMock,
        ) as mock_ctx,
        patch(
            "market_analysis.dynamic_rollover.DynamicRolloverEngine.evaluate_fundamental_thesis",
            new_callable=AsyncMock,
        ) as mock_eval,
    ):
        await cog._scan_one_symbol("AMD", {1001})

    mock_ctx.assert_not_called()
    mock_eval.assert_not_called()
    mock_bot.queue_dm.assert_not_called()


@pytest.mark.asyncio
async def test_scan_one_symbol_broken_dispatches_only_to_opted_in_holders(
    mock_bot: Any,
) -> None:
    cog = FundamentalFilingMonitorCog(mock_bot)
    result = FundamentalThesisResult(
        is_broken=True, confidence=0.9, reasoning="護城河流失"
    )

    def _notif_enabled(user_id: int, key: str) -> bool:
        assert key == "defense_fundamental_thesis"
        return user_id == 1001  # 只有 1001 開啟通知

    with (
        patch(
            "services.fundamental_service.get_fundamental_reports_list",
            new_callable=AsyncMock,
            return_value=[{"accession_number": "0002-33", "form": "8-K"}],
        ),
        patch(
            "database.market_cache.get_fundamental_scan_state",
            return_value=None,
        ),
        patch(
            "database.market_cache.save_fundamental_scan_state",
        ) as mock_save_state,
        patch(
            "services.fundamental_service.get_fundamental_context",
            new_callable=AsyncMock,
            return_value={
                "text": "CFO resigned",
                "source_url": "https://sec.gov/doc",
                "form_type": "8-K",
                "sections": {"key_events": "[Item 5.02] CFO resigned"},
            },
        ),
        patch(
            "market_analysis.dynamic_rollover.DynamicRolloverEngine.evaluate_fundamental_thesis",
            new_callable=AsyncMock,
            return_value=result,
        ) as mock_eval,
        patch("database.is_notification_enabled", side_effect=_notif_enabled),
    ):
        await cog._scan_one_symbol("AMD", {1001, 2002})

    mock_eval.assert_awaited_once_with(
        "AMD",
        "[SEC 財報段落]:\nCFO resigned\n",
        form_type="8-K",
        sections={"key_events": "[Item 5.02] CFO resigned"},
    )
    mock_save_state.assert_called_once_with("AMD", "0002-33", "8-K")
    mock_bot.queue_dm.assert_called_once()
    assert mock_bot.queue_dm.call_args[0][0] == 1001


@pytest.mark.asyncio
async def test_scan_one_symbol_passed_updates_cursor_without_dm(mock_bot: Any) -> None:
    cog = FundamentalFilingMonitorCog(mock_bot)
    result = FundamentalThesisResult(is_broken=False, confidence=0.8, reasoning="穩固")

    with (
        patch(
            "services.fundamental_service.get_fundamental_reports_list",
            new_callable=AsyncMock,
            return_value=[{"accession_number": "0003-44", "form": "10-K"}],
        ),
        patch("database.market_cache.get_fundamental_scan_state", return_value=None),
        patch("database.market_cache.save_fundamental_scan_state") as mock_save_state,
        patch(
            "services.fundamental_service.get_fundamental_context",
            new_callable=AsyncMock,
            return_value={
                "text": "全年營收成長",
                "source_url": "",
                "form_type": "10-K",
                "sections": {},
            },
        ),
        patch(
            "market_analysis.dynamic_rollover.DynamicRolloverEngine.evaluate_fundamental_thesis",
            new_callable=AsyncMock,
            return_value=result,
        ),
    ):
        await cog._scan_one_symbol("MSFT", {1001})

    mock_save_state.assert_called_once_with("MSFT", "0003-44", "10-K")
    mock_bot.queue_dm.assert_not_called()


@pytest.mark.asyncio
async def test_scan_one_symbol_llm_failure_leaves_cursor_untouched(
    mock_bot: Any,
) -> None:
    cog = FundamentalFilingMonitorCog(mock_bot)

    with (
        patch(
            "services.fundamental_service.get_fundamental_reports_list",
            new_callable=AsyncMock,
            return_value=[{"accession_number": "0004-55", "form": "10-Q"}],
        ),
        patch("database.market_cache.get_fundamental_scan_state", return_value=None),
        patch("database.market_cache.save_fundamental_scan_state") as mock_save_state,
        patch(
            "services.fundamental_service.get_fundamental_context",
            new_callable=AsyncMock,
            return_value={"text": "Q3 revenue", "source_url": "", "form_type": "10-Q"},
        ),
        patch(
            "market_analysis.dynamic_rollover.DynamicRolloverEngine.evaluate_fundamental_thesis",
            new_callable=AsyncMock,
            return_value=None,
        ),
    ):
        await cog._scan_one_symbol("NVDA", {1001})

    mock_save_state.assert_not_called()
    mock_bot.queue_dm.assert_not_called()


@pytest.mark.asyncio
async def test_scan_holdings_dedupes_symbol_across_multiple_holders(
    mock_bot: Any,
) -> None:
    """同一標的被兩位使用者持有時，_scan_one_symbol 只應被呼叫一次。"""
    cog = FundamentalFilingMonitorCog(mock_bot)

    with (
        patch(
            "database.get_all_holdings",
            return_value=_holdings((1001, "AMD"), (2002, "AMD")),
        ),
        patch.object(cog, "_scan_one_symbol", new_callable=AsyncMock) as mock_scan_one,
    ):
        await cog._scan_holdings_for_new_filings()

    mock_scan_one.assert_called_once()
    call_symbol, call_holders = mock_scan_one.call_args[0]
    assert call_symbol == "AMD"
    assert call_holders == {1001, 2002}
