"""after_market 備援路徑：report_failed 的使用者不得被渲染成「無持倉資料」。"""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from cogs.trading.after_market import AfterMarketCog


@pytest.mark.asyncio
async def test_report_failed_entry_is_skipped_and_counted() -> None:
    bot = MagicMock()
    bot.get_cog.return_value = None
    with patch("discord.ext.tasks.Loop.start"):
        cog = AfterMarketCog(bot)
    cog.trading_service = MagicMock()
    cog.trading_service.get_after_market_report_data = AsyncMock(
        return_value={5: {"report_failed": True}}
    )
    with (
        patch("cogs.trading.after_market.create_portfolio_report_embed") as mk_embed,
        patch("cogs.trading.after_market.database"),
    ):
        stats = await cog._run_after_market_report_pipeline(dry_run=True)
    mk_embed.assert_not_called()
    assert stats["users_failed"] == 1
    assert stats["errors"] == ["report_failed: uid=5"]
