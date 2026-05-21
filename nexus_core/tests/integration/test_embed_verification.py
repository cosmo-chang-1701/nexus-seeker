import pytest
from unittest.mock import patch
from bot import NexusBot, DISCORD_EMBED_DESCRIPTION_LIMIT, _split_discord_text
from cogs.terminal import TerminalCog
from cogs.embed_builder import create_info_embed


@pytest.fixture
def bot():
    return NexusBot()


@pytest.mark.asyncio
async def test_bot_queue_dm_auto_wraps_text(bot):
    """測試 bot.queue_dm 是否會將純文字自動包裝進 Embed"""
    user_id = 12345
    test_message = "Hello, this is a test message"

    with patch("bot.add_pending_notification") as mock_add:
        await bot.queue_dm(user_id, message=test_message)

        args, _ = mock_add.call_args
        assert args[0] == user_id
        assert args[1] is None
        assert args[2] is not None
        assert args[2]["title"] == "ℹ️ Nexus Seeker 通知"
        assert args[2]["description"] == test_message


@pytest.mark.asyncio
async def test_bot_queue_dm_keeps_embed(bot):
    """測試 bot.queue_dm 如果已經有 Embed，則保持原樣"""
    user_id = 12345
    test_embed = create_info_embed("Custom Title", "Custom Message")

    with patch("bot.add_pending_notification") as mock_add:
        await bot.queue_dm(user_id, embed=test_embed)

        args, _ = mock_add.call_args
        assert args[0] == user_id
        assert args[1] is None
        assert args[2] is not None
        assert args[2]["title"] == "ℹ️ Custom Title"
        assert args[2]["description"] == "Custom Message"


@pytest.mark.asyncio
async def test_bot_queue_dm_splits_long_text(bot):
    """測試超長純文字通知會自動切成多個安全 Embed。"""
    user_id = 12345
    test_message = "A" * (DISCORD_EMBED_DESCRIPTION_LIMIT + 100)

    with patch("bot.add_pending_notification") as mock_add:
        await bot.queue_dm(user_id, message=test_message)

        assert mock_add.call_count == 2

        first_args, _ = mock_add.call_args_list[0]
        second_args, _ = mock_add.call_args_list[1]

        assert first_args[0] == user_id
        assert first_args[1] is None
        assert second_args[1] is None
        assert len(first_args[2]["description"]) <= DISCORD_EMBED_DESCRIPTION_LIMIT
        assert len(second_args[2]["description"]) <= DISCORD_EMBED_DESCRIPTION_LIMIT
        assert (
            first_args[2]["description"] + second_args[2]["description"] == test_message
        )


@pytest.mark.asyncio
async def test_split_discord_text_preserves_code_blocks():
    """測試超長 ANSI code block 仍會維持合法 fence 格式。"""
    body = "A" * (DISCORD_EMBED_DESCRIPTION_LIMIT + 100)
    message = f"```ansi\n{body}\n```"

    chunks = _split_discord_text(message, DISCORD_EMBED_DESCRIPTION_LIMIT)

    assert len(chunks) == 2
    assert all(len(chunk) <= DISCORD_EMBED_DESCRIPTION_LIMIT for chunk in chunks)
    assert all(chunk.startswith("```ansi\n") for chunk in chunks)
    assert all(chunk.endswith("\n```") for chunk in chunks)


@pytest.mark.asyncio
async def test_terminal_cog_uses_embeds(mock_interaction, bot):
    """測試 TerminalCog 的指令是否使用 Embed 輸出"""
    terminal = TerminalCog(bot)
    await terminal.update_settings.callback(terminal, mock_interaction, capital=-100)

    assert mock_interaction.followup.send.called
    _, kwargs = mock_interaction.followup.send.call_args
    assert "embed" in kwargs
    assert kwargs["embed"].title == "❌ 系統錯誤"
    assert "資金必須大於 0" in kwargs["embed"].description


@pytest.mark.asyncio
async def test_terminal_cog_success_embed(mock_interaction, bot, db_conn):
    """測試 TerminalCog 指令成功時是否也使用 Embed"""
    terminal = TerminalCog(bot)
    await terminal.update_settings.callback(terminal, mock_interaction, capital=200000)

    assert mock_interaction.followup.send.called
    _, kwargs = mock_interaction.followup.send.call_args
    assert "embed" in kwargs
    assert kwargs["embed"].title == "ℹ️ 系統資訊"
    assert "帳戶設定已更新" in kwargs["embed"].description
