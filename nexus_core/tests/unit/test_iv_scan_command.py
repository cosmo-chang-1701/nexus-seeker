"""/iv_scan 只掃描、只回覆呼叫者自己的觀察清單。

過去它掃描全部使用者並把結果私訊給其他人，且沒有管理員檢查——任何人執行一次就會對
全體使用者發送不受通知開關控制的 DM。
"""

from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


@pytest.mark.asyncio
async def test_iv_scan_only_scans_and_replies_to_invoker() -> None:
    from cogs.trading.scanner_commands import ScannerCommandsCog

    bot = MagicMock()
    bot.queue_dm = AsyncMock()
    with patch("services.trading_service.TradingService"):
        cog = ScannerCommandsCog(bot)
    cog.trading_service = MagicMock()
    cog.trading_service.run_iv_opportunity_scan = AsyncMock(
        return_value=[{"symbol": "AAPL"}]
    )

    interaction: Any = MagicMock()
    interaction.user.id = 1
    interaction.response.defer = AsyncMock()
    interaction.followup.send = AsyncMock()

    watchlist = [(1, "AAPL", None), (2, "TSLA", None), (3, "NVDA", None)]
    with (
        patch("database.get_all_watchlist", return_value=watchlist),
        patch("cogs.embed_builder.create_volatility_embed", return_value=MagicMock()),
    ):
        callback: Any = ScannerCommandsCog.iv_scan.callback
        await callback(cog, interaction)

    cog.trading_service.run_iv_opportunity_scan.assert_awaited_once_with(["AAPL"], 1)
    bot.queue_dm.assert_not_awaited()
    interaction.followup.send.assert_awaited_once()


def test_execution_embed_for_dm_is_discord_embed() -> None:
    """NRO 掃描過去把 build_execution_embed() 的 dict 直接傳給 queue_dm，入列時
    `.to_dict()` 拋錯並中斷整輪掃描推播；私訊路徑必須拿到 discord.Embed。"""
    import discord

    from formatters.execution_embeds import (
        build_execution_discord_embed,
        build_execution_embed,
    )
    from models.execution import ExecutionDecision

    decision = ExecutionDecision(
        decision_type="STANDBY",
        trigger_reason="測試觀望",
        grid_params=None,
        position_sizing=None,
        exit_strategy=None,
    )
    embed = build_execution_discord_embed(decision)
    assert isinstance(embed, discord.Embed)
    assert embed.to_dict().get("title") == build_execution_embed(decision).get("title")
