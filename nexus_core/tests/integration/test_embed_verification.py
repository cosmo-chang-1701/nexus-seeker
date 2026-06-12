import pytest
from unittest.mock import patch
from bot import NexusBot, DISCORD_EMBED_DESCRIPTION_LIMIT, _split_discord_text
from cogs.terminal import TerminalCog
from cogs.embed_builder import create_info_embed


@pytest.fixture
def bot():
    # 測試 queue_dm/worker 行為時，需允許寫入通知佇列（避免 follower gate 直接跳過）
    inst = NexusBot()
    setattr(inst, "_is_leader_instance", True)
    return inst


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
    await terminal.update_settings.callback(terminal, mock_interaction, risk_limit=-100)

    assert mock_interaction.followup.send.called
    _, kwargs = mock_interaction.followup.send.call_args
    assert "embed" in kwargs
    assert kwargs["embed"].title == "❌ 系統錯誤"
    assert "風險限制需介於 1.0% 至 50.0% 之間" in kwargs["embed"].description


@pytest.mark.asyncio
async def test_terminal_cog_success_embed(mock_interaction, bot, db_conn):
    """測試 TerminalCog 指令成功時是否也使用 Embed"""
    terminal = TerminalCog(bot)
    await terminal.update_settings.callback(terminal, mock_interaction, risk_limit=25.0)

    assert mock_interaction.followup.send.called
    _, kwargs = mock_interaction.followup.send.call_args
    assert "embed" in kwargs
    assert kwargs["embed"].title == "ℹ️ 系統資訊"
    assert "帳戶設定已更新" in kwargs["embed"].description


@pytest.mark.asyncio
async def test_bot_queue_dm_splits_large_embed(bot):
    """測試當 Embed 總長度大於 5500 字元時，queue_dm 會自動將其拆分為多個通知並存入資料庫。"""
    import discord

    user_id = 12345
    large_embed = discord.Embed(title="Oversized Embed", description="Base description")
    # 增加大量 fields 使其長度超過 5500 字元
    for i in range(7):
        large_embed.add_field(name=f"Field {i}", value="A" * 900, inline=False)

    with patch("bot.add_pending_notification") as mock_add:
        await bot.queue_dm(user_id, message="Original Message Text", embed=large_embed)

        # 應該被合併拆分成 2 個獨立的欄位 Embed (而非 7 個零碎的)
        assert mock_add.call_count == 2

        # 檢查第一個通知
        first_args, _ = mock_add.call_args_list[0]
        assert first_args[0] == user_id
        assert first_args[1] == "Original Message Text"
        assert first_args[2] is not None
        assert "Field 0" in first_args[2]["fields"][0]["name"]

        # 檢查後續通知，其 message 欄位應該為 None (避免重複發送)
        second_args, _ = mock_add.call_args_list[1]
        assert second_args[0] == user_id
        assert second_args[1] is None
        assert second_args[2] is not None
        # 第二個通知應該包含後面剩餘的 fields
        assert any(
            f"Field {idx}" in second_args[2]["fields"][0]["name"] for idx in [5, 6]
        )


@pytest.mark.asyncio
async def test_message_worker_unblocks_on_http_400(bot):
    """測試當 _message_worker 遭遇 HTTP 400 Bad Request 時，會將該永久失敗的通知從資料庫刪除，以防阻塞佇列。"""
    import discord
    from unittest.mock import MagicMock, AsyncMock

    user_id = 12345
    notif_id = 999
    message = "Test message"
    embed_dict = {"title": "Test"}

    # Mock discord.HTTPException (status = 400)
    mock_resp = MagicMock()
    mock_resp.status = 400
    mock_resp.reason = "Bad Request"
    http_exc = discord.HTTPException(
        response=mock_resp, message="Embed size exceeds maximum size of 6000"
    )

    # Mock database functions & fetch_user
    mock_get_pending = MagicMock(
        return_value=[(notif_id, user_id, message, embed_dict)]
    )
    mock_delete = MagicMock()

    mock_user = MagicMock()
    mock_user.send = AsyncMock(side_effect=http_exc)
    mock_fetch_user = AsyncMock(return_value=mock_user)

    with patch("bot.get_pending_notifications", mock_get_pending), patch(
        "bot.delete_notification", mock_delete
    ), patch.object(bot, "fetch_user", mock_fetch_user), patch.object(
        bot, "wait_until_ready", AsyncMock()
    ), patch.object(
        bot, "is_closed", side_effect=[False, False, True, True, True]
    ):  # 執行一次 loop 後終止
        await bot._message_worker()

        # 應調用 fetch_user 及 user.send
        mock_fetch_user.assert_called_once_with(user_id)
        mock_user.send.assert_called_once()

        # 遭遇 HTTP 400 時，應立即刪除通知，以防阻塞
        mock_delete.assert_called_once_with(notif_id)
