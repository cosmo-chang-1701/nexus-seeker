"""
tests/test_dynamic_after_market_report.py

針對 cogs/trading.py 中 SchedulerCog.dynamic_after_market_report 的四種模擬情境測試:
1. test_multi_user_normal_report     — 多用戶持倉，正常報告分發
2. test_empty_portfolio_early_return  — 空持倉，提前返回不發私訊
3. test_forbidden_user_silent_fail    — 部分用戶私訊被拒 (discord.Forbidden) 不崩潰
4. test_empty_report_no_send         — check_portfolio_status_logic 回空報告，不發私訊
"""

import unittest
from unittest.mock import AsyncMock, MagicMock, patch
import asyncio
import sys
import os
from types import ModuleType
from datetime import datetime
from zoneinfo import ZoneInfo

# ====================================================================
# Mock heavy dependencies BEFORE importing the cog
# ====================================================================
# discord
mock_discord = MagicMock()
mock_discord.Embed = MagicMock
mock_discord.Color.gold.return_value = 0xFFD700
mock_discord.Forbidden = type('Forbidden', (Exception,), {})

# Create a real-looking app_commands mock
mock_app_commands = MagicMock()
mock_discord.app_commands = mock_app_commands

# discord.ext.tasks — need tasks.loop to return a decorator that preserves the coroutine
real_tasks = MagicMock()

def fake_loop(**kwargs):
    """Decorator that wraps the coroutine but adds .start/.cancel/.before_loop attrs."""
    def decorator(func):
        func.start = MagicMock()
        func.cancel = MagicMock()
        func.before_loop = lambda f: f  # no-op decorator
        return func
    return decorator

real_tasks.loop = fake_loop

mock_ext = MagicMock()
mock_ext.tasks = real_tasks
mock_ext.commands = MagicMock()
mock_ext.commands.Cog = type('Cog', (), {})

# Register discord mocks
sys.modules.setdefault("discord", mock_discord)
sys.modules.setdefault("discord.ext", mock_ext)
sys.modules.setdefault("discord.ext.tasks", real_tasks)
sys.modules.setdefault("discord.ext.commands", mock_ext.commands)
sys.modules.setdefault("discord.app_commands", mock_app_commands)

# Other project dependencies
sys.modules.setdefault("database", MagicMock())
sys.modules.setdefault("market_math", MagicMock())
sys.modules.setdefault("market_time", MagicMock())
sys.modules.setdefault("market_analysis", MagicMock())
sys.modules.setdefault("market_analysis.portfolio", MagicMock())

# cogs.embed_builder — only mock the submodule, NOT the 'cogs' package itself
mock_embed_builder = MagicMock()
mock_embed_builder.create_scan_embed = MagicMock()
sys.modules.setdefault("cogs.embed_builder", mock_embed_builder)

# config
if "config" not in sys.modules:
    mock_config = ModuleType("config")
    mock_config.RISK_FREE_RATE = 0.042
    mock_config.DB_NAME = ":memory:"
    sys.modules["config"] = mock_config

# Ensure project root is in sys.path
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from cogs.trading import SchedulerCog

ny_tz = ZoneInfo("America/New_York")


class TestDynamicAfterMarketReport(unittest.TestCase):
    """針對 dynamic_after_market_report 的四種模擬情境"""

    def _make_cog(self):
        """建構一個 SchedulerCog，但跳過 __init__ 中的 .start() 副作用"""
        bot = MagicMock()
        bot.wait_until_ready = AsyncMock()
        bot.fetch_user = AsyncMock()
        bot.notify_all_users = AsyncMock()
        cog = object.__new__(SchedulerCog)
        cog.bot = bot
        return cog

    # ----------------------------------------------------------------
    # Scenario 1: 多用戶正常報告分發
    # ----------------------------------------------------------------
    @patch("cogs.trading.market_analysis.portfolio.check_portfolio_status_logic")
    @patch("cogs.trading.database.get_user_capital")
    @patch("cogs.trading.database.get_all_portfolio")
    @patch("cogs.trading.asyncio.sleep", new_callable=AsyncMock)
    @patch("cogs.trading.market_time.get_sleep_seconds", return_value=0)
    @patch("cogs.trading.market_time.get_next_market_target_time")
    def test_multi_user_normal_report(
        self, mock_target_time, mock_sleep_secs, mock_async_sleep,
        mock_get_all, mock_get_capital, mock_check_logic
    ):
        """兩位用戶各持有不同部位 → 各收到一則含 Embed 的私訊"""
        cog = self._make_cog()

        # Mock: market_time 回傳一個固定時間
        mock_target_time.return_value = datetime(2025, 6, 10, 16, 15, tzinfo=ny_tz)

        # Mock: 全站持倉 (user_id, id, symbol, opt_type, strike, expiry, entry_price, quantity)
        mock_get_all.return_value = [
            (1001, 1, "AAPL", "call", 200.0, "2025-07-18", 5.00, -1),
            (1001, 2, "TSLA", "put",  250.0, "2025-07-18", 3.00, -2),
            (2002, 3, "NVDA", "call", 900.0, "2025-08-15", 8.00,  1),
        ]

        # Mock: 使用者資金
        mock_get_capital.return_value = 100000.0

        # Mock: 結算引擎回傳報告行
        mock_check_logic.return_value = ["**AAPL** 報告行", "**TSLA** 報告行"]

        # Mock: bot.fetch_user 回傳可發送訊息的 mock user
        user_a = AsyncMock()
        user_b = AsyncMock()
        cog.bot.fetch_user = AsyncMock(side_effect=lambda uid: {1001: user_a, 2002: user_b}[uid])

        # 執行
        asyncio.run(SchedulerCog.dynamic_after_market_report(cog))

        # 驗證
        self.assertEqual(mock_get_all.call_count, 1)
        self.assertEqual(mock_check_logic.call_count, 2)  # 兩位用戶各算一次
        user_a.send.assert_called_once()
        user_b.send.assert_called_once()

    # ----------------------------------------------------------------
    # Scenario 2: 空持倉提前返回
    # ----------------------------------------------------------------
    @patch("cogs.trading.database.get_all_portfolio")
    @patch("cogs.trading.asyncio.sleep", new_callable=AsyncMock)
    @patch("cogs.trading.market_time.get_sleep_seconds", return_value=0)
    @patch("cogs.trading.market_time.get_next_market_target_time")
    def test_empty_portfolio_early_return(
        self, mock_target_time, mock_sleep_secs, mock_async_sleep, mock_get_all
    ):
        """get_all_portfolio() 回傳空列表 → 提前 return，不呼叫 fetch_user"""
        cog = self._make_cog()
        mock_target_time.return_value = datetime(2025, 6, 10, 16, 15, tzinfo=ny_tz)
        mock_get_all.return_value = []

        asyncio.run(SchedulerCog.dynamic_after_market_report(cog))

        mock_get_all.assert_called_once()
        cog.bot.fetch_user.assert_not_called()

    # ----------------------------------------------------------------
    # Scenario 3: 私訊被拒 (discord.Forbidden) 不崩潰
    # ----------------------------------------------------------------
    @patch("cogs.trading.market_analysis.portfolio.check_portfolio_status_logic")
    @patch("cogs.trading.database.get_user_capital")
    @patch("cogs.trading.database.get_all_portfolio")
    @patch("cogs.trading.asyncio.sleep", new_callable=AsyncMock)
    @patch("cogs.trading.market_time.get_sleep_seconds", return_value=0)
    @patch("cogs.trading.market_time.get_next_market_target_time")
    def test_forbidden_user_silent_fail(
        self, mock_target_time, mock_sleep_secs, mock_async_sleep,
        mock_get_all, mock_get_capital, mock_check_logic
    ):
        """用戶 B 的 user.send() 拋出 discord.Forbidden → 不崩潰，用戶 A 正常收到"""
        cog = self._make_cog()
        mock_target_time.return_value = datetime(2025, 6, 10, 16, 15, tzinfo=ny_tz)

        # 兩位用戶各一筆持倉
        mock_get_all.return_value = [
            (1001, 1, "AAPL", "put", 180.0, "2025-07-18", 4.00, -1),
            (2002, 2, "MSFT", "call", 400.0, "2025-07-18", 6.00, 1),
        ]
        mock_get_capital.return_value = 50000.0
        mock_check_logic.return_value = ["報告行"]

        # 用戶 A: 正常送達 / 用戶 B: 拋出 Forbidden
        user_a = AsyncMock()
        user_b = AsyncMock()

        # 取得正確的 Forbidden exception class
        import discord as _discord
        user_b.send.side_effect = _discord.Forbidden

        cog.bot.fetch_user = AsyncMock(side_effect=lambda uid: {1001: user_a, 2002: user_b}[uid])

        # 不應拋出例外
        try:
            asyncio.run(SchedulerCog.dynamic_after_market_report(cog))
        except Exception as exc:
            self.fail(f"dynamic_after_market_report raised {type(exc).__name__}: {exc}")

        # 用戶 A 正常收到
        user_a.send.assert_called_once()
        # 用戶 B 的 send 也被呼叫了 (但拋了 Forbidden)
        user_b.send.assert_called_once()

    # ----------------------------------------------------------------
    # Scenario 4: 空報告不發私訊
    # ----------------------------------------------------------------
    @patch("cogs.trading.market_analysis.portfolio.check_portfolio_status_logic")
    @patch("cogs.trading.database.get_user_capital")
    @patch("cogs.trading.database.get_all_portfolio")
    @patch("cogs.trading.asyncio.sleep", new_callable=AsyncMock)
    @patch("cogs.trading.market_time.get_sleep_seconds", return_value=0)
    @patch("cogs.trading.market_time.get_next_market_target_time")
    def test_empty_report_no_send(
        self, mock_target_time, mock_sleep_secs, mock_async_sleep,
        mock_get_all, mock_get_capital, mock_check_logic
    ):
        """check_portfolio_status_logic 回空列表 → 不呼叫 user.send()"""
        cog = self._make_cog()
        mock_target_time.return_value = datetime(2025, 6, 10, 16, 15, tzinfo=ny_tz)

        mock_get_all.return_value = [
            (1001, 1, "AAPL", "call", 200.0, "2025-07-18", 5.00, -1),
        ]
        mock_get_capital.return_value = 100000.0

        # 結算引擎回傳空列表 → 無報告行
        mock_check_logic.return_value = []

        mock_user = AsyncMock()
        cog.bot.fetch_user = AsyncMock(return_value=mock_user)

        asyncio.run(SchedulerCog.dynamic_after_market_report(cog))

        # check_portfolio_status_logic 被呼叫但因為回傳空，所以不該 fetch_user
        mock_check_logic.assert_called_once()
        cog.bot.fetch_user.assert_not_called()
        mock_user.send.assert_not_called()


if __name__ == '__main__':
    unittest.main()
