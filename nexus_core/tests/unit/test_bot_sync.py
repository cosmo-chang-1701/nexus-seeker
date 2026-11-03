"""
nexus_core/tests/unit/test_bot_sync.py

Tests for startup tree sync removal, background sync scheduling, and !sync admin prefix command.
"""

from typing import Any
from unittest.mock import AsyncMock, MagicMock, PropertyMock, patch
import asyncio
import pytest

from bot import NexusBot
from cogs.trading.admin_commands import AdminCommandsCog
from config import DISCORD_ADMIN_USER_ID


def _create_test_cog(mock_bot: MagicMock) -> AdminCommandsCog:
    """Helper to instantiate AdminCommandsCog and bind command cog reference."""
    cog = AdminCommandsCog(mock_bot)
    # discord.py sets Command.cog when cog is loaded by bot
    cog.sync.cog = cog
    return cog


@pytest.mark.asyncio
async def test_setup_hook_does_not_call_tree_sync() -> None:
    """Verify that setup_hook() completes without calling self.tree.sync()."""
    mock_tree = MagicMock()
    mock_tree.sync = AsyncMock()

    with patch("discord.Intents.default"), patch(
        "discord.ext.commands.Bot.__init__", return_value=None
    ), patch.object(
        NexusBot, "tree", new_callable=PropertyMock, return_value=mock_tree
    ):
        bot = NexusBot()
        bot.loop = MagicMock()

        with patch.object(bot, "load_extension", new_callable=AsyncMock), patch.object(
            bot, "_message_worker", new_callable=MagicMock
        ), patch.object(bot, "_health_worker", new_callable=MagicMock), patch(
            "services.memory_manager.MemoryManager"
        ), patch("services.hedge_monitor_service.HedgeMonitorService"), patch(
            "services.polymarket_service.PolymarketService"
        ), patch("services.alpaca_stream_service.AlpacaStreamService"), patch(
            "services.alpaca_stream_service.set_stream_service"
        ):
            await bot.setup_hook()

        # Crucial check: tree.sync() must NOT be called in setup_hook
        mock_tree.sync.assert_not_called()
        mock_tree.sync.assert_not_awaited()
        assert bot._setup_done is True


@pytest.mark.asyncio
async def test_on_ready_schedules_background_sync() -> None:
    """Verify that on_ready() dispatches _sync_commands_background non-blockingly."""
    with patch("discord.Intents.default"), patch(
        "discord.ext.commands.Bot.__init__", return_value=None
    ), patch.object(
        NexusBot, "user", new_callable=PropertyMock, return_value="NexusBot#0001"
    ):
        bot = NexusBot()
        bot.loop = MagicMock()
        assert bot._has_synced_commands is False

        created_tasks: list[Any] = []

        def capture_create_task(coro: Any) -> MagicMock:
            created_tasks.append(coro)
            if hasattr(coro, "close"):
                coro.close()
            return MagicMock()

        with patch("asyncio.to_thread", new_callable=AsyncMock), patch(
            "database.init_db"
        ), patch("database.connection.DatabaseWriteQueue.initialize"), patch(
            "database.leader_lock.try_acquire_leader_lock", return_value=False
        ), patch.object(bot, "_leader_lock_loop", return_value=None), patch.object(
            bot, "_sync_commands_background", new_callable=AsyncMock
        ) as mock_sync_bg, patch(
            "asyncio.create_task", side_effect=capture_create_task
        ):
            await bot.on_ready()

            assert bot._has_synced_commands is True
            mock_sync_bg.assert_called_once()
            assert len(created_tasks) > 0


@pytest.mark.asyncio
async def test_on_ready_reconnect_skips_reschedule() -> None:
    """Verify that bot reconnection in on_ready() skips rescheduling background sync."""
    with patch("discord.Intents.default"), patch(
        "discord.ext.commands.Bot.__init__", return_value=None
    ):
        bot = NexusBot()
        bot._has_notified_ready = True
        bot._has_synced_commands = True

        with patch("asyncio.create_task") as mock_create_task, patch.object(
            bot, "_sync_commands_background", new_callable=AsyncMock
        ) as mock_sync_bg:
            await bot.on_ready()

            mock_sync_bg.assert_not_called()
            mock_create_task.assert_not_called()


@pytest.mark.asyncio
async def test_sync_commands_background_success() -> None:
    """Verify _sync_commands_background sleeps briefly and calls tree.sync()."""
    mock_tree = MagicMock()
    synced_cmds = [MagicMock(name="cmd1"), MagicMock(name="cmd2")]
    mock_tree.sync = AsyncMock(return_value=synced_cmds)

    with patch("discord.Intents.default"), patch(
        "discord.ext.commands.Bot.__init__", return_value=None
    ), patch.object(
        NexusBot, "tree", new_callable=PropertyMock, return_value=mock_tree
    ):
        bot = NexusBot()

        with patch("asyncio.sleep", new_callable=AsyncMock) as mock_sleep:
            await bot._sync_commands_background()

            mock_sleep.assert_awaited_once_with(3)
            mock_tree.sync.assert_awaited_once_with()


@pytest.mark.asyncio
async def test_sync_commands_background_handles_exception() -> None:
    """Verify _sync_commands_background handles tree.sync() exceptions gracefully."""
    mock_tree = MagicMock()
    mock_tree.sync = AsyncMock(side_effect=RuntimeError("Discord API 500 error"))

    with patch("discord.Intents.default"), patch(
        "discord.ext.commands.Bot.__init__", return_value=None
    ), patch.object(
        NexusBot, "tree", new_callable=PropertyMock, return_value=mock_tree
    ):
        bot = NexusBot()

        with patch("asyncio.sleep", new_callable=AsyncMock):
            # Should not raise exception
            await bot._sync_commands_background()
            mock_tree.sync.assert_awaited_once()


@pytest.mark.asyncio
async def test_sync_commands_background_exits_if_closing() -> None:
    """Verify _sync_commands_background returns early if bot is closing after sleep."""
    mock_tree = MagicMock()
    mock_tree.sync = AsyncMock()

    with patch("discord.Intents.default"), patch(
        "discord.ext.commands.Bot.__init__", return_value=None
    ), patch.object(
        NexusBot, "tree", new_callable=PropertyMock, return_value=mock_tree
    ):
        bot = NexusBot()

        async def fake_sleep(seconds: float) -> None:
            bot._is_closing = True

        with patch("asyncio.sleep", side_effect=fake_sleep):
            await bot._sync_commands_background()
            mock_tree.sync.assert_not_called()


@pytest.mark.asyncio
async def test_sync_commands_background_propagates_cancellation() -> None:
    """Verify _sync_commands_background re-raises asyncio.CancelledError cleanly."""
    mock_tree = MagicMock()
    mock_tree.sync = AsyncMock()

    with patch("discord.Intents.default"), patch(
        "discord.ext.commands.Bot.__init__", return_value=None
    ), patch.object(
        NexusBot, "tree", new_callable=PropertyMock, return_value=mock_tree
    ):
        bot = NexusBot()

        async def cancel_sleep(seconds: float) -> None:
            raise asyncio.CancelledError()

        with patch("asyncio.sleep", side_effect=cancel_sleep):
            with pytest.raises(asyncio.CancelledError):
                await bot._sync_commands_background()
            mock_tree.sync.assert_not_called()


@pytest.mark.asyncio
async def test_admin_sync_unauthorized() -> None:
    """Verify !sync command rejects non-admin users with error embed and warning."""
    mock_bot = MagicMock()
    mock_bot.tree = MagicMock()
    mock_bot.tree.sync = AsyncMock()
    cog = _create_test_cog(mock_bot)

    ctx = MagicMock()
    ctx.bot = mock_bot
    ctx.author.id = DISCORD_ADMIN_USER_ID + 9999  # Different ID
    ctx.author.name = "unauthorized_tester"
    ctx.reply = AsyncMock()

    await cog.sync(ctx, spec=None)

    mock_bot.tree.sync.assert_not_called()
    ctx.reply.assert_awaited_once()
    call_kwargs = ctx.reply.await_args.kwargs
    embed = call_kwargs.get("embed")
    assert embed is not None
    assert "權限不足" in str(embed.description)


@pytest.mark.asyncio
async def test_admin_sync_global_success() -> None:
    """Verify !sync command without spec syncs global tree."""
    mock_bot = MagicMock()
    mock_bot.tree = MagicMock()
    synced_cmds = [MagicMock(), MagicMock(), MagicMock()]
    mock_bot.tree.sync = AsyncMock(return_value=synced_cmds)
    cog = _create_test_cog(mock_bot)

    ctx = MagicMock()
    ctx.bot = mock_bot
    ctx.author.id = DISCORD_ADMIN_USER_ID
    ctx.author.name = "admin_tester"
    ctx.reply = AsyncMock()

    await cog.sync(ctx, spec=None)

    mock_bot.tree.sync.assert_awaited_once_with()
    ctx.reply.assert_awaited_once()
    call_kwargs = ctx.reply.await_args.kwargs
    embed = call_kwargs.get("embed")
    assert embed is not None
    assert "成功同步 3 個指令" in str(embed.description)
    assert "全局" in str(embed.description)


@pytest.mark.asyncio
async def test_admin_sync_guild_success() -> None:
    """Verify !sync guild copies global commands and syncs guild tree."""
    mock_bot = MagicMock()
    mock_bot.tree = MagicMock()
    mock_bot.tree.copy_global_to = MagicMock()
    synced_cmds = [MagicMock()]
    mock_bot.tree.sync = AsyncMock(return_value=synced_cmds)
    cog = _create_test_cog(mock_bot)

    ctx = MagicMock()
    ctx.bot = mock_bot
    ctx.author.id = DISCORD_ADMIN_USER_ID
    ctx.author.name = "admin_tester"
    mock_guild = MagicMock()
    mock_guild.name = "TestGuild"
    ctx.guild = mock_guild
    ctx.reply = AsyncMock()

    await cog.sync(ctx, spec="guild")

    mock_bot.tree.copy_global_to.assert_called_once_with(guild=mock_guild)
    mock_bot.tree.sync.assert_awaited_once_with(guild=mock_guild)
    ctx.reply.assert_awaited_once()
    embed = ctx.reply.await_args.kwargs.get("embed")
    assert embed is not None
    assert "成功同步 1 個指令" in str(embed.description)
    assert "guild" in str(embed.description)


@pytest.mark.asyncio
async def test_admin_sync_guild_shortcut_tilde() -> None:
    """Verify !sync ~ behaves identically to !sync guild."""
    mock_bot = MagicMock()
    mock_bot.tree = MagicMock()
    mock_bot.tree.copy_global_to = MagicMock()
    synced_cmds = [MagicMock(), MagicMock()]
    mock_bot.tree.sync = AsyncMock(return_value=synced_cmds)
    cog = _create_test_cog(mock_bot)

    ctx = MagicMock()
    ctx.bot = mock_bot
    ctx.author.id = DISCORD_ADMIN_USER_ID
    ctx.author.name = "admin_tester"
    mock_guild = MagicMock()
    ctx.guild = mock_guild
    ctx.reply = AsyncMock()

    await cog.sync(ctx, spec="~")

    mock_bot.tree.copy_global_to.assert_called_once_with(guild=mock_guild)
    mock_bot.tree.sync.assert_awaited_once_with(guild=mock_guild)
    embed = ctx.reply.await_args.kwargs.get("embed")
    assert embed is not None
    assert "成功同步 2 個指令" in str(embed.description)
    assert "~" in str(embed.description)


@pytest.mark.asyncio
async def test_admin_sync_guild_in_dm_fails() -> None:
    """Verify !sync guild fails when run in DM (ctx.guild is None)."""
    mock_bot = MagicMock()
    mock_bot.tree = MagicMock()
    mock_bot.tree.sync = AsyncMock()
    cog = _create_test_cog(mock_bot)

    ctx = MagicMock()
    ctx.bot = mock_bot
    ctx.author.id = DISCORD_ADMIN_USER_ID
    ctx.author.name = "admin_tester"
    ctx.guild = None
    ctx.reply = AsyncMock()

    await cog.sync(ctx, spec="guild")

    mock_bot.tree.sync.assert_not_called()
    ctx.reply.assert_awaited_once()
    embed = ctx.reply.await_args.kwargs.get("embed")
    assert embed is not None
    assert "必須在伺服器" in str(embed.description)


@pytest.mark.asyncio
async def test_admin_sync_clear_success() -> None:
    """Verify !sync clear clears commands and syncs guild tree."""
    mock_bot = MagicMock()
    mock_bot.tree = MagicMock()
    mock_bot.tree.clear_commands = MagicMock()
    mock_bot.tree.sync = AsyncMock(return_value=[])
    cog = _create_test_cog(mock_bot)

    ctx = MagicMock()
    ctx.bot = mock_bot
    ctx.author.id = DISCORD_ADMIN_USER_ID
    ctx.author.name = "admin_tester"
    mock_guild = MagicMock()
    ctx.guild = mock_guild
    ctx.reply = AsyncMock()

    await cog.sync(ctx, spec="clear")

    mock_bot.tree.clear_commands.assert_called_once_with(guild=mock_guild)
    mock_bot.tree.sync.assert_awaited_once_with(guild=mock_guild)
    ctx.reply.assert_awaited_once()
    embed = ctx.reply.await_args.kwargs.get("embed")
    assert embed is not None
    assert "clear" in str(embed.description)


@pytest.mark.asyncio
async def test_admin_sync_clear_in_dm_fails() -> None:
    """Verify !sync clear fails when run in DM (ctx.guild is None)."""
    mock_bot = MagicMock()
    mock_bot.tree = MagicMock()
    mock_bot.tree.sync = AsyncMock()
    cog = _create_test_cog(mock_bot)

    ctx = MagicMock()
    ctx.bot = mock_bot
    ctx.author.id = DISCORD_ADMIN_USER_ID
    ctx.author.name = "admin_tester"
    ctx.guild = None
    ctx.reply = AsyncMock()

    await cog.sync(ctx, spec="clear")

    mock_bot.tree.sync.assert_not_called()
    ctx.reply.assert_awaited_once()
    embed = ctx.reply.await_args.kwargs.get("embed")
    assert embed is not None
    assert "必須在伺服器" in str(embed.description)


@pytest.mark.asyncio
async def test_admin_sync_invalid_spec() -> None:
    """Verify !sync with invalid spec returns error without calling sync()."""
    mock_bot = MagicMock()
    mock_bot.tree = MagicMock()
    mock_bot.tree.sync = AsyncMock()
    cog = _create_test_cog(mock_bot)

    ctx = MagicMock()
    ctx.bot = mock_bot
    ctx.author.id = DISCORD_ADMIN_USER_ID
    ctx.author.name = "admin_tester"
    ctx.reply = AsyncMock()

    await cog.sync(ctx, spec="unknown_mode")

    mock_bot.tree.sync.assert_not_called()
    ctx.reply.assert_awaited_once()
    embed = ctx.reply.await_args.kwargs.get("embed")
    assert embed is not None
    assert "未知的同步參數" in str(embed.description)


@pytest.mark.asyncio
async def test_admin_sync_handles_exception() -> None:
    """Verify !sync catches exceptions from tree.sync and sends error embed."""
    mock_bot = MagicMock()
    mock_bot.tree = MagicMock()
    mock_bot.tree.sync = AsyncMock(side_effect=RuntimeError("Discord API Error 500"))
    cog = _create_test_cog(mock_bot)

    ctx = MagicMock()
    ctx.bot = mock_bot
    ctx.author.id = DISCORD_ADMIN_USER_ID
    ctx.author.name = "admin_tester"
    ctx.reply = AsyncMock()

    await cog.sync(ctx, spec=None)

    ctx.reply.assert_awaited_once()
    embed = ctx.reply.await_args.kwargs.get("embed")
    assert embed is not None
    assert "同步指令失敗" in str(embed.description)
