import sys
import os
import pytest
from unittest.mock import MagicMock, patch

# Ensure we can import from nexus_core
sys.path.append(os.path.join(os.getcwd(), "nexus_core"))

def test_imports():
    """Verify that all core modules and services can be imported without error."""
    import bot
    import main
    import config
    import database.core
    import services.calendar_service
    import services.llm_service
    import services.market_data_service
    import market_analysis.risk_engine
    import market_analysis.strategy
    import market_analysis.sentiment_engine

def test_bot_initialization():
    """Verify that NexusBot can be instantiated and cogs can be loaded (initially)."""
    from bot import NexusBot
    
    # Mock discord.py internals to avoid connecting to API
    with patch("discord.Intents.default"), \
         patch("discord.ext.commands.Bot.__init__", return_value=None):
        bot_inst = NexusBot()
        assert bot_inst is not None

@pytest.mark.asyncio
async def test_cog_loading_structure():
    """Verify Cog classes are structurally valid by trying to instantiate them with a mock bot."""
    from cogs.unified_terminal import UnifiedTerminalCog
    from cogs.terminal import TerminalCog
    from cogs.trading import SchedulerCog
    from cogs.analyst_agent import AnalystAgent
    from cogs.calendar import CalendarCog
    
    mock_bot = MagicMock()
    
    # Test instantiation of each cog
    # Note: Some cogs might start tasks in __init__, so we patch those if necessary
    with patch("discord.ext.tasks.Loop.start"):
        cogs = [
            UnifiedTerminalCog(mock_bot),
            TerminalCog(mock_bot),
            SchedulerCog(mock_bot),
            AnalystAgent(mock_bot),
            CalendarCog(mock_bot)
        ]
        for cog in cogs:
            assert cog is not None
