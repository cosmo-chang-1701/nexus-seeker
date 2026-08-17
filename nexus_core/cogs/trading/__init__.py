"""
cogs/trading — 重構後的 trading package shim。
保留向後相容性：`from cogs.trading import SchedulerCog` 仍可正常運作。
"""

from cogs.trading.scheduler import SchedulerCog

__all__ = ["SchedulerCog"]


async def setup(bot):  # type: ignore
    """由 bot.py 透過 load_extension("cogs.trading") 呼叫，統一載入所有子 Cog。"""
    from cogs.trading.scan import setup as setup_scan
    from cogs.trading.scheduler import setup as setup_scheduler
    from cogs.trading.pre_market import setup as setup_pre_market
    from cogs.trading.portfolio_monitor import setup as setup_portfolio_monitor
    from cogs.trading.telemetry import setup as setup_telemetry
    from cogs.trading.after_market import setup as setup_after_market
    from cogs.trading.admin_commands import setup as setup_admin
    from cogs.trading.scanner_commands import setup as setup_scanner
    from cogs.trading.wti_monitor import setup as setup_wti_monitor

    # MarketScanCog must be added before SchedulerCog so get_cog("MarketScanCog") works
    await setup_scan(bot)
    await setup_scheduler(bot)
    await setup_pre_market(bot)
    await setup_portfolio_monitor(bot)
    await setup_telemetry(bot)
    await setup_after_market(bot)
    await setup_admin(bot)
    await setup_scanner(bot)
    await setup_wti_monitor(bot)
