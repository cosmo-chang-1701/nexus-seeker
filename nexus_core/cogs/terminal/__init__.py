"""cogs/terminal — 重構後的 terminal package shim。
保留向後相容性：`from cogs.terminal import TerminalCog` 仍可正常運作。"""

from typing import Any

from .cog import TerminalCog
from cogs.settings_ui import (
    SETTINGS_LABELS,  # noqa: F401
    NotificationSettingsView,  # noqa: F401
    AccountSettingsModal,  # noqa: F401
    AccountSettingsView,  # noqa: F401
)

__all__ = ["TerminalCog"]


async def setup(bot: Any) -> None:
    """由 bot.py 透過 load_extension("cogs.terminal") 呼叫。"""
    await bot.add_cog(TerminalCog(bot))
