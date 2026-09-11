from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch
import pytest
import sqlite3

from services.asset_manager import (
    AssetManager,
    WatchlistLimitExceededError,
    _MAX_WATCHLIST_SYMBOLS_PER_USER,
)
from models.asset import Asset, ContextType
from cogs.terminal import TerminalCog
from cogs.embed_builders.watchlist_embeds import create_set_watchlist_result_embed
from database.watchlist import set_user_watchlist


def test_set_watchlist_normal(db_conn: Any) -> None:
    """測試 set_watchlist 能正常覆蓋舊觀察清單並回傳清除與新增數量。"""
    user_id = 777001
    manager = AssetManager()

    # 先加入 2 檔既有標的
    manager.add_asset(
        Asset(
            user_id=user_id,
            symbol="OLD1",
            context_type=ContextType.WATCH,
            metadata={},
        )
    )
    manager.add_asset(
        Asset(
            user_id=user_id,
            symbol="OLD2",
            context_type=ContextType.WATCH,
            metadata={},
        )
    )

    cleared_count, new_symbols = manager.set_watchlist(
        user_id, ["AAPL", "TSLA", "NVDA"]
    )

    assert cleared_count == 2
    assert new_symbols == ["AAPL", "TSLA", "NVDA"]

    current_assets = manager.get_assets(user_id, ContextType.WATCH)
    assert [a.symbol for a in current_assets] == ["AAPL", "TSLA", "NVDA"]


def test_set_watchlist_deduplication_and_casing(db_conn: Any) -> None:
    """測試 set_watchlist 自動去重、剔除空白並轉大寫。"""
    user_id = 777002
    manager = AssetManager()

    cleared_count, new_symbols = manager.set_watchlist(
        user_id, ["aapl", "  msft  ", "AAPL", "msft", "GOOG"]
    )

    assert cleared_count == 0
    assert new_symbols == ["AAPL", "MSFT", "GOOG"]

    current_assets = manager.get_assets(user_id, ContextType.WATCH)
    assert [a.symbol for a in current_assets] == ["AAPL", "MSFT", "GOOG"]


def test_set_watchlist_preserves_other_contexts_and_users(db_conn: Any) -> None:
    """測試 set_watchlist 只刪除該使用者的 WATCH 標的，不影響 TRADE/HOLDING 或其他使用者。"""
    user_a = 777003
    user_b = 777004
    manager = AssetManager()

    # User A 的 WATCH, TRADE, HOLDING
    manager.add_asset(
        Asset(
            user_id=user_a,
            symbol="A_WATCH",
            context_type=ContextType.WATCH,
            metadata={},
        )
    )
    manager.add_asset(
        Asset(
            user_id=user_a,
            symbol="A_TRADE",
            context_type=ContextType.TRADE,
            metadata={},
        )
    )
    manager.add_asset(
        Asset(
            user_id=user_a,
            symbol="A_HOLD",
            context_type=ContextType.HOLDING,
            metadata={},
        )
    )

    # User B 的 WATCH
    manager.add_asset(
        Asset(
            user_id=user_b,
            symbol="B_WATCH",
            context_type=ContextType.WATCH,
            metadata={},
        )
    )

    # User A 執行 set_watchlist
    cleared, new_syms = manager.set_watchlist(user_a, ["NEW_SYM"])
    assert cleared == 1
    assert new_syms == ["NEW_SYM"]

    # User A 狀態檢驗
    a_watch = manager.get_assets(user_a, ContextType.WATCH)
    assert [a.symbol for a in a_watch] == ["NEW_SYM"]

    a_trade = manager.get_assets(user_a, ContextType.TRADE)
    assert [a.symbol for a in a_trade] == ["A_TRADE"]

    a_hold = manager.get_assets(user_a, ContextType.HOLDING)
    assert [a.symbol for a in a_hold] == ["A_HOLD"]

    # User B 狀態檢驗 (完全不受影響)
    b_watch = manager.get_assets(user_b, ContextType.WATCH)
    assert [b.symbol for b in b_watch] == ["B_WATCH"]


def test_set_watchlist_limit_exceeded(db_conn: Any) -> None:
    """測試有效標的超過 50 檔時拋出 WatchlistLimitExceededError 且資料庫未變更。"""
    user_id = 777005
    manager = AssetManager()

    manager.add_asset(
        Asset(
            user_id=user_id,
            symbol="EXISTING",
            context_type=ContextType.WATCH,
            metadata={},
        )
    )

    too_many_symbols: list[str] = [
        f"SYM{i}" for i in range(_MAX_WATCHLIST_SYMBOLS_PER_USER + 1)
    ]

    with pytest.raises(WatchlistLimitExceededError):
        manager.set_watchlist(user_id, too_many_symbols)

    # 原清單保持原狀
    current = manager.get_assets(user_id, ContextType.WATCH)
    assert [a.symbol for a in current] == ["EXISTING"]


def test_set_watchlist_atomic_rollback_on_error(db_conn: Any) -> None:
    """測試在插入標的過程發生例外時，交易自動回滾，原清單完整保留。"""
    user_id = 777006
    manager = AssetManager()

    manager.add_asset(
        Asset(
            user_id=user_id,
            symbol="ORIGINAL_1",
            context_type=ContextType.WATCH,
            metadata={},
        )
    )
    manager.add_asset(
        Asset(
            user_id=user_id,
            symbol="ORIGINAL_2",
            context_type=ContextType.WATCH,
            metadata={},
        )
    )

    original_conn_getter = manager._get_conn

    class CursorProxy:
        def __init__(self, real_cursor: Any) -> None:
            self._real = real_cursor

        @property
        def rowcount(self) -> int:
            return self._real.rowcount  # type: ignore

        def execute(self, sql: str, *params: Any) -> Any:
            if "INSERT INTO assets" in sql:
                raise sqlite3.OperationalError("Simulated database failure")
            return self._real.execute(sql, *params)

        def fetchall(self) -> Any:
            return self._real.fetchall()

        def fetchone(self) -> Any:
            return self._real.fetchone()

    class ConnProxy:
        def __init__(self, real_conn: Any) -> None:
            self._real = real_conn

        def cursor(self) -> Any:
            return CursorProxy(self._real.cursor())

        def commit(self) -> None:
            self._real.commit()

        def rollback(self) -> None:
            self._real.rollback()

        def close(self) -> None:
            self._real.close()

        def __enter__(self) -> Any:
            return self

        def __exit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> Any:
            if exc_type is not None:
                self.rollback()
                return False
            self.commit()
            return True

    def faulty_conn_getter() -> Any:
        return ConnProxy(original_conn_getter())

    with patch.object(manager, "_get_conn", side_effect=faulty_conn_getter):
        with pytest.raises(sqlite3.OperationalError):
            manager.set_watchlist(user_id, ["NEW_FAIL"])

    # 驗證原標的仍完好存在（交易回滾）
    assets = manager.get_assets(user_id, ContextType.WATCH)
    assert {a.symbol for a in assets} == {"ORIGINAL_1", "ORIGINAL_2"}


def test_database_set_user_watchlist(db_conn: Any) -> None:
    """測試 database.watchlist.set_user_watchlist 封裝函式。"""
    user_id = 777007
    cleared, syms = set_user_watchlist(user_id, ["AMD", "INTC"])
    assert cleared == 0
    assert syms == ["AMD", "INTC"]

    manager = AssetManager()
    assets = manager.get_assets(user_id, ContextType.WATCH)
    assert [a.symbol for a in assets] == ["AMD", "INTC"]


def test_create_set_watchlist_result_embed() -> None:
    """測試結果 Embed 輸出內容是否符合需求。"""
    embed_full = create_set_watchlist_result_embed(
        succeeded=["AAPL", "TSLA"],
        cleared_count=3,
        invalid=["BAD1", "BAD2"],
    )
    desc_full = embed_full.description or ""
    assert "已設定觀察清單" in desc_full
    assert "(2 檔)" in desc_full
    assert "AAPL, TSLA" in desc_full
    assert "已清除原清單" in desc_full
    assert "3 檔標的" in desc_full
    assert "無效代號" in desc_full
    assert "(2 檔)" in desc_full
    assert "BAD1, BAD2" in desc_full

    embed_clean = create_set_watchlist_result_embed(
        succeeded=["NVDA"],
        cleared_count=0,
        invalid=None,
    )
    desc_clean = embed_clean.description or ""
    assert "已設定觀察清單" in desc_clean
    assert "(1 檔): NVDA" in desc_clean
    assert "已清除原清單" in desc_clean
    assert "0 檔標的" in desc_clean
    assert "無效代號" not in desc_clean


@pytest.mark.asyncio
async def test_set_watch_slash_normal_replacement(
    mock_interaction: Any, db_conn: Any
) -> None:
    """AC1: 執行 /set_watch symbol:'AAPL, TSLA, NVDA' 能將 WATCH 替換為 ['AAPL', 'TSLA', 'NVDA']。"""
    manager = AssetManager()
    manager.add_asset(
        Asset(
            user_id=mock_interaction.user.id,
            symbol="OLD_SYM",
            context_type=ContextType.WATCH,
            metadata={},
        )
    )

    bot = MagicMock()
    bot.wait_until_ready = AsyncMock()
    terminal = TerminalCog(bot)

    await terminal.set_watch.callback(  # type: ignore
        terminal,  # type: ignore
        mock_interaction,
        symbol="AAPL, TSLA, NVDA",
    )

    embed = mock_interaction.followup.send.call_args.kwargs["embed"]
    description = embed.description or ""
    assert "已設定觀察清單" in description
    assert "AAPL, TSLA, NVDA" in description
    assert "已清除原清單" in description
    assert "1 檔標的" in description

    current = manager.get_assets(mock_interaction.user.id, ContextType.WATCH)
    assert [a.symbol for a in current] == ["AAPL", "TSLA", "NVDA"]


@pytest.mark.asyncio
async def test_set_watch_slash_dedup_and_normalize(
    mock_interaction: Any, db_conn: Any
) -> None:
    """AC1: 執行 /set_watch symbol:'aapl msft   aapl' 能正確去重並正規化為大寫 ['AAPL', 'MSFT']。"""
    bot = MagicMock()
    bot.wait_until_ready = AsyncMock()
    terminal = TerminalCog(bot)

    await terminal.set_watch.callback(  # type: ignore
        terminal,  # type: ignore
        mock_interaction,
        symbol="aapl msft   aapl",
    )

    embed = mock_interaction.followup.send.call_args.kwargs["embed"]
    description = embed.description or ""
    assert "已設定觀察清單" in description
    assert "AAPL, MSFT" in description

    manager = AssetManager()
    current = manager.get_assets(mock_interaction.user.id, ContextType.WATCH)
    assert [a.symbol for a in current] == ["AAPL", "MSFT"]


@pytest.mark.asyncio
async def test_set_watch_slash_empty_input(mock_interaction: Any, db_conn: Any) -> None:
    """AC2: 輸入空白字串時中止操作並回報錯誤，現存清單維持原狀。"""
    manager = AssetManager()
    manager.add_asset(
        Asset(
            user_id=mock_interaction.user.id,
            symbol="KEEP_ME",
            context_type=ContextType.WATCH,
            metadata={},
        )
    )

    bot = MagicMock()
    bot.wait_until_ready = AsyncMock()
    terminal = TerminalCog(bot)

    await terminal.set_watch.callback(  # type: ignore
        terminal,  # type: ignore
        mock_interaction,
        symbol="   ,  ",
    )

    embed = mock_interaction.followup.send.call_args.kwargs["embed"]
    description = embed.description or ""
    assert "請輸入至少一個有效的股票代號" in description

    current = manager.get_assets(mock_interaction.user.id, ContextType.WATCH)
    assert [a.symbol for a in current] == ["KEEP_ME"]


@pytest.mark.asyncio
async def test_set_watch_slash_all_invalid(mock_interaction: Any, db_conn: Any) -> None:
    """AC2: 輸入全為無效代號時中止操作並回報錯誤，現存清單維持原狀。"""
    manager = AssetManager()
    manager.add_asset(
        Asset(
            user_id=mock_interaction.user.id,
            symbol="ORIGINAL",
            context_type=ContextType.WATCH,
            metadata={},
        )
    )

    bot = MagicMock()
    bot.wait_until_ready = AsyncMock()
    terminal = TerminalCog(bot)

    with patch(
        "services.market_data_service.validate_symbol",
        new_callable=AsyncMock,
        return_value=False,
    ):
        await terminal.set_watch.callback(  # type: ignore
            terminal,  # type: ignore
            mock_interaction,
            symbol="FAKE1, FAKE2",
        )

    embed = mock_interaction.followup.send.call_args.kwargs["embed"]
    description = embed.description or ""
    assert "所有輸入的標的代號皆無效" in description
    assert "FAKE1, FAKE2" in description
    assert "現有觀察清單未變更" in description

    current = manager.get_assets(mock_interaction.user.id, ContextType.WATCH)
    assert [a.symbol for a in current] == ["ORIGINAL"]


@pytest.mark.asyncio
async def test_set_watch_slash_exceeding_limit(
    mock_interaction: Any, db_conn: Any
) -> None:
    """AC2: 輸入有效代號超過 50 檔時拋出超額警告並拒絕覆蓋，原清單保持不變。"""
    manager = AssetManager()
    manager.add_asset(
        Asset(
            user_id=mock_interaction.user.id,
            symbol="ORIGINAL_50",
            context_type=ContextType.WATCH,
            metadata={},
        )
    )

    bot = MagicMock()
    bot.wait_until_ready = AsyncMock()
    terminal = TerminalCog(bot)

    many_symbols = " ".join([f"SYM{i}" for i in range(51)])

    await terminal.set_watch.callback(  # type: ignore
        terminal,  # type: ignore
        mock_interaction,
        symbol=many_symbols,
    )

    embed = mock_interaction.followup.send.call_args.kwargs["embed"]
    description = embed.description or ""
    assert "超過觀察清單上限 (50 檔)" in description
    assert "現有觀察清單維持不變" in description

    current = manager.get_assets(mock_interaction.user.id, ContextType.WATCH)
    assert [a.symbol for a in current] == ["ORIGINAL_50"]


@pytest.mark.asyncio
async def test_set_watch_slash_partial_invalid(
    mock_interaction: Any, db_conn: Any
) -> None:
    """AC2: 輸入部分有效、部分無效代號時，清楚提示哪些代號無效，並將有效代號完成原子覆蓋。"""
    manager = AssetManager()
    manager.add_asset(
        Asset(
            user_id=mock_interaction.user.id,
            symbol="BEFORE",
            context_type=ContextType.WATCH,
            metadata={},
        )
    )

    bot = MagicMock()
    bot.wait_until_ready = AsyncMock()
    terminal = TerminalCog(bot)

    async def mock_val(s: str) -> bool:
        return s in {"AAPL", "MSFT"}

    with patch(
        "services.market_data_service.validate_symbol",
        side_effect=mock_val,
    ):
        await terminal.set_watch.callback(  # type: ignore
            terminal,  # type: ignore
            mock_interaction,
            symbol="AAPL, BADSYM, MSFT",
        )

    embed = mock_interaction.followup.send.call_args.kwargs["embed"]
    description = embed.description or ""
    assert "已設定觀察清單" in description
    assert "AAPL, MSFT" in description
    assert "已清除原清單" in description
    assert "1 檔標的" in description
    assert "無效代號" in description
    assert "BADSYM" in description

    current = manager.get_assets(mock_interaction.user.id, ContextType.WATCH)
    assert [a.symbol for a in current] == ["AAPL", "MSFT"]


@pytest.mark.asyncio
async def test_set_watch_slash_chinese_delimiters_and_cashtags(
    mock_interaction: Any, db_conn: Any
) -> None:
    """測試支援全形中文逗號、全形空格、cashtag $ 去除與大小寫去重。"""
    bot = MagicMock()
    bot.wait_until_ready = AsyncMock()
    terminal = TerminalCog(bot)

    await terminal.set_watch.callback(  # type: ignore
        terminal,  # type: ignore
        mock_interaction,
        symbol="$AAPL， $tsla　NVDA,  $AAPL",
    )

    embed = mock_interaction.followup.send.call_args.kwargs["embed"]
    description = embed.description or ""
    assert "已設定觀察清單" in description
    assert "AAPL, TSLA, NVDA" in description

    manager = AssetManager()
    current = manager.get_assets(mock_interaction.user.id, ContextType.WATCH)
    assert [a.symbol for a in current] == ["AAPL", "TSLA", "NVDA"]


@pytest.mark.asyncio
async def test_set_watch_slash_unconventional_punctuation_all_invalid(
    mock_interaction: Any, db_conn: Any
) -> None:
    """對抗性測試：輸入含非法符號（如分號、斜線）的異常代號，驗證能優雅報錯並維持原清單。"""
    manager = AssetManager()
    manager.add_asset(
        Asset(
            user_id=mock_interaction.user.id,
            symbol="PRESERVED",
            context_type=ContextType.WATCH,
            metadata={},
        )
    )

    bot = MagicMock()
    bot.wait_until_ready = AsyncMock()
    terminal = TerminalCog(bot)

    from services.market_data_service.quote import (
        validate_symbol as real_validate_symbol,
    )

    with patch(
        "services.market_data_service.validate_symbol",
        side_effect=real_validate_symbol,
    ):
        await terminal.set_watch.callback(  # type: ignore
            terminal,  # type: ignore
            mock_interaction,
            symbol="AAPL;TSLA/NVDA",
        )

    embed = mock_interaction.followup.send.call_args.kwargs["embed"]
    description = embed.description or ""
    assert "所有輸入的標的代號皆無效" in description
    assert "AAPL;TSLA/NVDA" in description
    assert "現有觀察清單未變更" in description

    current = manager.get_assets(mock_interaction.user.id, ContextType.WATCH)
    assert [a.symbol for a in current] == ["PRESERVED"]


@pytest.mark.asyncio
async def test_set_watch_slash_unconventional_punctuation_mixed_valid(
    mock_interaction: Any, db_conn: Any
) -> None:
    """對抗性測試：混合合法與含分號/斜線之非法代號，驗證明確提示無效項目並原子覆蓋有效標的。"""
    manager = AssetManager()
    manager.add_asset(
        Asset(
            user_id=mock_interaction.user.id,
            symbol="OLD",
            context_type=ContextType.WATCH,
            metadata={},
        )
    )

    bot = MagicMock()
    bot.wait_until_ready = AsyncMock()
    terminal = TerminalCog(bot)

    from services.market_data_service.quote import (
        validate_symbol as real_validate_symbol,
    )

    with patch(
        "services.market_data_service.validate_symbol",
        side_effect=real_validate_symbol,
    ):
        await terminal.set_watch.callback(  # type: ignore
            terminal,  # type: ignore
            mock_interaction,
            symbol="AAPL, TSLA;NVDA, $MSFT",
        )

    embed = mock_interaction.followup.send.call_args.kwargs["embed"]
    description = embed.description or ""
    assert "已設定觀察清單" in description
    assert "AAPL, MSFT" in description
    assert "已清除原清單" in description
    assert "1 檔標的" in description
    assert "無效代號" in description
    assert "TSLA;NVDA" in description

    current = manager.get_assets(mock_interaction.user.id, ContextType.WATCH)
    assert [a.symbol for a in current] == ["AAPL", "MSFT"]


def test_set_watchlist_cashtag_cleaning_direct(db_conn: Any) -> None:
    """測試 AssetManager.set_watchlist 直接接收包含 $ 符號之標的時能防禦性清理。"""
    user_id = 777008
    manager = AssetManager()
    cleared, syms = manager.set_watchlist(user_id, ["$AAPL", "$msft", "GOOG"])
    assert cleared == 0
    assert syms == ["AAPL", "MSFT", "GOOG"]

    current = manager.get_assets(user_id, ContextType.WATCH)
    assert [a.symbol for a in current] == ["AAPL", "MSFT", "GOOG"]


@pytest.mark.asyncio
async def test_set_watch_slash_chinese_dunhao_fullwidth_cashtag_and_letters(
    mock_interaction: Any, db_conn: Any
) -> None:
    """測試支援中文頓號 (、)、全形 cashtag ＄、全形英文字母 (ＡＡＰＬ) 與全形逗號之混合輸入。"""
    bot = MagicMock()
    bot.wait_until_ready = AsyncMock()
    terminal = TerminalCog(bot)

    await terminal.set_watch.callback(  # type: ignore
        terminal,  # type: ignore
        mock_interaction,
        symbol="＄ＡＡＰＬ、  $tsla　NVDA，  ＄AAPL",
    )

    embed = mock_interaction.followup.send.call_args.kwargs["embed"]
    description = embed.description or ""
    assert "已設定觀察清單" in description
    assert "AAPL, TSLA, NVDA" in description

    manager = AssetManager()
    current = manager.get_assets(mock_interaction.user.id, ContextType.WATCH)
    assert [a.symbol for a in current] == ["AAPL", "TSLA", "NVDA"]


@pytest.mark.asyncio
async def test_set_watch_slash_validation_exception_resilience(
    mock_interaction: Any, db_conn: Any
) -> None:
    """測試當單一標的驗證過程拋出非預期例外時，能優雅視為無效代號而不中斷整體流程。"""
    manager = AssetManager()
    bot = MagicMock()
    bot.wait_until_ready = AsyncMock()
    terminal = TerminalCog(bot)

    async def mock_val_with_exc(s: str) -> bool:
        if s == "CRASH":
            raise RuntimeError("Unexpected network error")
        return s in {"AAPL", "MSFT"}

    with patch(
        "services.market_data_service.validate_symbol",
        side_effect=mock_val_with_exc,
    ):
        await terminal.set_watch.callback(  # type: ignore
            terminal,  # type: ignore
            mock_interaction,
            symbol="AAPL, CRASH, MSFT",
        )

    embed = mock_interaction.followup.send.call_args.kwargs["embed"]
    description = embed.description or ""
    assert "已設定觀察清單" in description
    assert "AAPL, MSFT" in description
    assert "無效代號" in description
    assert "CRASH" in description

    current = manager.get_assets(mock_interaction.user.id, ContextType.WATCH)
    assert [a.symbol for a in current] == ["AAPL", "MSFT"]


def test_set_watchlist_direct_fullwidth_cleaning(db_conn: Any) -> None:
    """測試 AssetManager.set_watchlist 直接接收全形字元及全形 cashtag 能防禦性正規化。"""
    user_id = 777009
    manager = AssetManager()
    cleared, syms = manager.set_watchlist(
        user_id, ["＄ＡＡＰＬ", "＄tsla", "  ＧＯＯＧ  "]
    )
    assert cleared == 0
    assert syms == ["AAPL", "TSLA", "GOOG"]

    current = manager.get_assets(user_id, ContextType.WATCH)
    assert [a.symbol for a in current] == ["AAPL", "TSLA", "GOOG"]
