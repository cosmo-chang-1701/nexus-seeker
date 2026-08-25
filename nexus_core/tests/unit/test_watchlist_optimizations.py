from typing import Any
import pytest
from unittest.mock import AsyncMock, MagicMock

from services.asset_manager import (
    AssetManager,
    WatchlistLimitExceededError,
    _MAX_WATCHLIST_SYMBOLS_PER_USER,
)
from models.asset import Asset, ContextType
from cogs.terminal import TerminalCog
from cogs.embed_builders.watchlist_embeds import (
    create_bulk_watchlist_result_embed,
    _classify_watchlist_cache_tag,
    _format_watchlist_price_info,
)
from ui.watchlist import _apply_sort_and_filter


def test_watchlist_cap_enforced_only_for_watch_context(db_conn: Any) -> None:
    """超過上限時 WATCH 應被擋下，但 TRADE 情境不受此上限影響。"""
    user_id = 555001
    manager = AssetManager()

    for i in range(_MAX_WATCHLIST_SYMBOLS_PER_USER):
        asset = Asset(
            user_id=user_id,
            symbol=f"SYM{i}",
            context_type=ContextType.WATCH,
            metadata={},
        )
        assert manager.add_asset(asset) is True

    overflow_asset = Asset(
        user_id=user_id,
        symbol="OVERFLOW",
        context_type=ContextType.WATCH,
        metadata={},
    )
    with pytest.raises(WatchlistLimitExceededError):
        manager.add_asset(overflow_asset)

    # TRADE context 不受觀察清單上限影響
    trade_asset = Asset(
        user_id=user_id,
        symbol="OVERFLOW",
        context_type=ContextType.TRADE,
        metadata={
            "opt_type": "call",
            "strike": 100.0,
            "expiry": "2026-06-18",
            "entry_price": 1.0,
            "quantity": 1,
        },
    )
    assert manager.add_asset(trade_asset) is True


@pytest.mark.asyncio
async def test_add_watch_bulk_multi_symbol(mock_interaction: Any, db_conn: Any) -> None:
    """輸入多檔代號時應批次新增並回傳批次結果 Embed。"""
    bot = MagicMock()
    bot.wait_until_ready = AsyncMock()
    terminal = TerminalCog(bot)

    await terminal.add_watch.callback(  # type: ignore
        terminal,  # type: ignore
        mock_interaction,
        symbol="AAPL, TSLA NVDA",
    )

    embed = mock_interaction.followup.send.call_args.kwargs["embed"]
    description = embed.description or ""
    assert "AAPL" in description
    assert "TSLA" in description
    assert "NVDA" in description

    manager = AssetManager()
    assets = manager.get_assets(mock_interaction.user.id, ContextType.WATCH)
    assert {a.symbol for a in assets} == {"AAPL", "TSLA", "NVDA"}


@pytest.mark.asyncio
async def test_add_watch_bulk_reports_duplicates(
    mock_interaction: Any, db_conn: Any
) -> None:
    manager = AssetManager()
    manager.add_asset(
        Asset(
            user_id=mock_interaction.user.id,
            symbol="AAPL",
            context_type=ContextType.WATCH,
            metadata={},
        )
    )

    bot = MagicMock()
    bot.wait_until_ready = AsyncMock()
    terminal = TerminalCog(bot)

    await terminal.add_watch.callback(  # type: ignore
        terminal,  # type: ignore
        mock_interaction,
        symbol="AAPL, MSFT",
    )

    embed = mock_interaction.followup.send.call_args.kwargs["embed"]
    description = embed.description or ""
    assert "MSFT" in description
    assert "已存在" in description
    assert "AAPL" in description


@pytest.mark.asyncio
async def test_remove_watch_bulk_multi_symbol(
    mock_interaction: Any, db_conn: Any
) -> None:
    manager = AssetManager()
    for sym in ("AAPL", "TSLA", "NVDA"):
        manager.add_asset(
            Asset(
                user_id=mock_interaction.user.id,
                symbol=sym,
                context_type=ContextType.WATCH,
                metadata={},
            )
        )

    bot = MagicMock()
    bot.wait_until_ready = AsyncMock()
    terminal = TerminalCog(bot)

    await terminal.remove_watch.callback(  # type: ignore
        terminal,  # type: ignore
        mock_interaction,
        symbol="AAPL,TSLA",
    )

    embed = mock_interaction.followup.send.call_args.kwargs["embed"]
    description = embed.description or ""
    assert "AAPL" in description
    assert "TSLA" in description

    remaining = manager.get_assets(mock_interaction.user.id, ContextType.WATCH)
    assert {a.symbol for a in remaining} == {"NVDA"}


def test_apply_sort_and_filter_alpha_and_query() -> None:
    data = [
        ("TSLA", "CORE", None),
        ("AAPL", "TECH", None),
        ("NVDA", "TECH,CORE", None),
    ]
    sorted_alpha = _apply_sort_and_filter(data, "alpha", None)
    assert [row[0] for row in sorted_alpha] == ["AAPL", "NVDA", "TSLA"]

    filtered = _apply_sort_and_filter(data, "alpha", "tech")
    assert {row[0] for row in filtered} == {"AAPL", "NVDA"}

    filtered_by_symbol = _apply_sort_and_filter(data, None, "tsla")
    assert [row[0] for row in filtered_by_symbol] == ["TSLA"]


def test_classify_watchlist_cache_tag_thresholds() -> None:
    # 超跌磁吸: 跌破 expected_move_lower 且偏離 > 5%
    assert (
        _classify_watchlist_cache_tag(spot=90.0, max_pain=100.0, em_lower=95.0) == "🚀"
    )
    # 偏離超過 10% 但未跌破 em_lower -> 警示
    assert (
        _classify_watchlist_cache_tag(spot=115.0, max_pain=100.0, em_lower=None) == "⚠️"
    )
    # 偏離在門檻內 -> 無標籤
    assert (
        _classify_watchlist_cache_tag(spot=102.0, max_pain=100.0, em_lower=None) == ""
    )


def test_format_watchlist_price_info_degrades_without_cache(db_conn: Any) -> None:
    assert _format_watchlist_price_info("ZZZZ_NOT_CACHED") == "-- (尚未預熱)"


def test_create_bulk_watchlist_result_embed_no_success() -> None:
    embed = create_bulk_watchlist_result_embed("加入", [], {"無效代號": ["FAKE"]})
    description = embed.description or ""
    assert "沒有任何標的被加入" in description
    assert "FAKE" in description
