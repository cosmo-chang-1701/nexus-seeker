from .cog import UnifiedTerminalCog
from .symbol_view import SymbolHubView
from .portfolio_view import PortfolioHubView
from .pulse_view import PulseHubView
from .batch_scan_view import BatchScanView, BatchScanWarningButton
from .utils import get_macro_overview_data, find_matching_polymarket_odds
from cogs.embed_builder import (
    create_error_embed,
    build_radar_scan_embed,
    create_strategic_dash_embed,
    build_market_macro_overview_embed,
    create_tactical_symbol_embed,
    create_polymarket_list_embed,
)

__all__ = [
    "UnifiedTerminalCog",
    "SymbolHubView",
    "PortfolioHubView",
    "PulseHubView",
    "BatchScanView",
    "BatchScanWarningButton",
    "get_macro_overview_data",
    "find_matching_polymarket_odds",
    "create_error_embed",
    "build_radar_scan_embed",
    "create_strategic_dash_embed",
    "build_market_macro_overview_embed",
    "create_tactical_symbol_embed",
    "create_polymarket_list_embed",
]


async def setup(bot):
    await bot.add_cog(UnifiedTerminalCog(bot))
