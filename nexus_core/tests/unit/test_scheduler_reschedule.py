import asyncio
import unittest
from datetime import datetime, timedelta
from unittest.mock import AsyncMock, MagicMock, patch, PropertyMock
from zoneinfo import ZoneInfo

ny_tz = ZoneInfo("America/New_York")


class TestReschedulePreMarketRisk(unittest.IsolatedAsyncioTestCase):
    """驗證 _reschedule_pre_market_risk 正確設定 change_interval。"""

    def _make_cog(self):
        """建立一個最小化的 SchedulerCog mock，避免觸發真實 tasks.loop 啟動。"""
        from cogs.trading import SchedulerCog

        with patch.object(SchedulerCog, "__init__", lambda self, bot: None):
            cog = SchedulerCog.__new__(SchedulerCog)
            cog.bot = MagicMock()
            cog.bot.wait_until_ready = AsyncMock()
            cog.bot.notify_all_users = AsyncMock()

            # Mock tasks.loop 物件上的 change_interval
            cog.pre_market_risk_monitor = MagicMock()
            cog.pre_market_risk_monitor.change_interval = MagicMock()
            cog.dynamic_after_market_report = MagicMock()
            cog.dynamic_after_market_report.change_interval = MagicMock()
            return cog

    @patch("cogs.trading.market_time")
    async def test_reschedule_sets_interval_to_sleep_seconds(self, mock_mt):
        """正常情境：計算出目標時間後，change_interval 設為對應秒數。"""
        cog = self._make_cog()
        future_time = datetime.now(ny_tz) + timedelta(hours=5)
        mock_mt.get_next_market_target_time.return_value = future_time
        mock_mt.get_sleep_seconds.return_value = 18000.0  # 5 hours

        result = await cog._reschedule_pre_market_risk()

        self.assertEqual(result, future_time)
        cog.pre_market_risk_monitor.change_interval.assert_called_once_with(
            seconds=18000.0
        )

    @patch("cogs.trading.market_time")
    async def test_reschedule_fallback_on_no_target(self, mock_mt):
        """找不到目標時間時，fallback 設為 1 小時後重試。"""
        cog = self._make_cog()
        mock_mt.get_next_market_target_time.return_value = None

        result = await cog._reschedule_pre_market_risk()

        self.assertIsNone(result)
        cog.pre_market_risk_monitor.change_interval.assert_called_once_with(hours=1)

    @patch("cogs.trading.market_time")
    async def test_reschedule_enforces_minimum_30_seconds(self, mock_mt):
        """即使 sleep_seconds 極小，也不低於 30 秒，避免高頻空轉。"""
        cog = self._make_cog()
        future_time = datetime.now(ny_tz) + timedelta(seconds=5)
        mock_mt.get_next_market_target_time.return_value = future_time
        mock_mt.get_sleep_seconds.return_value = 5.0

        await cog._reschedule_pre_market_risk()

        cog.pre_market_risk_monitor.change_interval.assert_called_once_with(
            seconds=30.0
        )


class TestRescheduleAfterMarketReport(unittest.IsolatedAsyncioTestCase):
    """驗證 _reschedule_after_market_report 的 change_interval 邏輯。"""

    def _make_cog(self):
        from cogs.trading import SchedulerCog

        with patch.object(SchedulerCog, "__init__", lambda self, bot: None):
            cog = SchedulerCog.__new__(SchedulerCog)
            cog.bot = MagicMock()
            cog.bot.wait_until_ready = AsyncMock()
            cog.bot.notify_all_users = AsyncMock()
            cog.dynamic_after_market_report = MagicMock()
            cog.dynamic_after_market_report.change_interval = MagicMock()
            cog.pre_market_risk_monitor = MagicMock()
            return cog

    @patch("cogs.trading.market_time")
    async def test_reschedule_sets_correct_interval(self, mock_mt):
        """正常情境：計算出目標時間後，change_interval 設為對應秒數。"""
        cog = self._make_cog()
        future_time = datetime.now(ny_tz) + timedelta(hours=3)
        mock_mt.get_next_market_target_time.return_value = future_time
        mock_mt.get_sleep_seconds.return_value = 10800.0

        result = await cog._reschedule_after_market_report()

        self.assertEqual(result, future_time)
        cog.dynamic_after_market_report.change_interval.assert_called_once_with(
            seconds=10800.0
        )

    @patch("cogs.trading.market_time")
    async def test_reschedule_fallback_on_no_target(self, mock_mt):
        """找不到目標時間時，fallback 設為 1 小時後重試。"""
        cog = self._make_cog()
        mock_mt.get_next_market_target_time.return_value = None

        result = await cog._reschedule_after_market_report()

        self.assertIsNone(result)
        cog.dynamic_after_market_report.change_interval.assert_called_once_with(hours=1)


class TestBeforeLoopNoSleep(unittest.IsolatedAsyncioTestCase):
    """驗證 before_loop 不再包含 asyncio.sleep 呼叫。"""

    def test_before_loop_source_has_no_asyncio_sleep(self):
        """靜態檢查：before_loop 方法的原始碼中不應包含 asyncio.sleep。"""
        import inspect
        from cogs.trading import SchedulerCog

        before_pre_src = inspect.getsource(
            SchedulerCog.before_pre_market_risk_monitor
        )
        before_after_src = inspect.getsource(
            SchedulerCog.before_dynamic_after_market_report
        )

        self.assertNotIn("asyncio.sleep", before_pre_src)
        self.assertNotIn("asyncio.sleep", before_after_src)
