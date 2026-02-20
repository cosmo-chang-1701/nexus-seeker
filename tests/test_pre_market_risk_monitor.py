import unittest
from unittest.mock import AsyncMock, patch, MagicMock
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
import discord

from cogs.trading import SchedulerCog

ny_tz = ZoneInfo("America/New_York")

class TestPreMarketRiskMonitor(unittest.IsolatedAsyncioTestCase):
    
    # 🌟 關鍵修正 1：改用 asyncSetUp
    async def asyncSetUp(self):
        self.bot = AsyncMock()
        
        # 🌟 關鍵修正 2：直接攔截 discord.ext.tasks.Loop 的底層 start 方法
        # 這樣無論 Cog 怎麼複製並綁定 task，都不會真的啟動背景迴圈
        self.patcher_task_start = patch('discord.ext.tasks.Loop.start')
        self.mock_task_start = self.patcher_task_start.start()
        self.addCleanup(self.patcher_task_start.stop)

        # 現在在 Event Loop 環境內實例化 Cog
        self.cog = SchedulerCog(self.bot)

        # Mock market_time logic
        self.patcher_market_time = patch('cogs.trading.market_time')
        self.mock_market_time = self.patcher_market_time.start()
        self.addCleanup(self.patcher_market_time.stop)
        
        # Patch sleep to fast-forward execution
        self.patcher_sleep = patch('cogs.trading.asyncio.sleep', new_callable=AsyncMock)
        self.mock_sleep = self.patcher_sleep.start()
        self.addCleanup(self.patcher_sleep.stop)

        self.mock_now = datetime(2026, 2, 20, 9, 0, tzinfo=ny_tz)
        
        self.mock_user = AsyncMock()
        self.bot.fetch_user.return_value = self.mock_user

    async def test_01_empty_port_and_watch(self):
        """測試案例 1：資料庫中沒有使用者持倉及觀察清單"""
        with patch('cogs.trading.database.get_all_portfolio', return_value=[]), \
             patch('cogs.trading.database.get_all_watchlist', return_value=[]):
            
            # 不 mock datetime，直接執行
            await self.cog.pre_market_risk_monitor.coro(self.cog)
            
            self.bot.fetch_user.assert_not_called()
            self.mock_user.send.assert_not_called()

    async def test_02_symbols_with_no_earnings_risk(self):
        """測試案例 2：有持倉及觀察清單，但財報日距離大於 3 天 (標的皆為安全)"""
        # Update fake_port to match get_all_portfolio schema: (user_id, id, symbol, opt_type, strike, expiry, entry_price, quantity, is_covered)
        fake_port = [(1, 1, "AAPL", "CALL", 150.0, "2026-03-20", 5.0, 1, False)]
        # Update fake_watch to match get_all_watchlist schema: (user_id, symbol, is_covered)
        fake_watch = [(1, "MSFT", False)]
        
        with patch('cogs.trading.database.get_all_portfolio', return_value=fake_port), \
             patch('cogs.trading.database.get_all_watchlist', return_value=fake_watch), \
             patch('cogs.trading.yf.Ticker', return_value=MagicMock()), \
             patch('cogs.trading.market_math.get_next_earnings_date') as mock_earnings:
            
            # 💡 核心解法：直接用真實時間去推算 10 天後，讓代碼自己去算相對距離
            real_now = datetime.now(ny_tz)
            mock_earnings.return_value = (real_now + timedelta(days=10)).date()
            
            await self.cog.pre_market_risk_monitor.coro(self.cog)
            
            self.bot.fetch_user.assert_called_once_with(1)
            self.mock_user.send.assert_called_once()
            
            embed = self.mock_user.send.call_args.kwargs.get('embed')
            self.assertEqual(embed.color, discord.Color.green())

    async def test_03_symbols_with_earnings_risk(self):
        """
        測試案例 3：有持倉及觀察清單，且財報日在 3 天以內 (標的具風險)
        預期行為：機器人應發送紅色警告 Embed 給使用者，並標示倒數天數。
        """
        # 1. 準備假資料：模擬使用者 ID 為 2，持有 TSLA，觀察 NVDA
        fake_port = [(2, 2, "TSLA", "PUT", 200.0, "2026-03-20", 10.0, 1, False)]
        fake_watch = [(2, "NVDA", False)]
        
        # 2. Mock 掉外部依賴：資料庫與 YF API
        with patch('cogs.trading.database.get_all_portfolio', return_value=fake_port), \
             patch('cogs.trading.database.get_all_watchlist', return_value=fake_watch), \
             patch('cogs.trading.yf.Ticker', return_value=MagicMock()), \
             patch('cogs.trading.market_math.get_next_earnings_date') as mock_earnings:
            
            # 💡 核心邏輯：動態計算出「2 天後」的日期作為假財報日
            # 這樣不管測試哪一天跑，算出來的差距永遠是 2 天，且不會破壞 datetime 的 isinstance 判斷
            real_now = datetime.now(ny_tz)
            mock_earnings.return_value = (real_now + timedelta(days=2)).date()
            
            # 3. 執行目標函式 (手動觸發盤前掃描)
            await self.cog.pre_market_risk_monitor.coro(self.cog)
            
            # 4. 驗證結果：確認是否有去抓取 User ID 2 並發送訊息
            self.bot.fetch_user.assert_called_once_with(2)
            self.mock_user.send.assert_called_once()
            
            # 5. 深入驗證 Embed 內容是否符合「高風險預警」的規格
            embed = self.mock_user.send.call_args.kwargs.get('embed')
            self.assertIsNotNone(embed, "必須發送 Embed 訊息")
            
            # 檢查標題與顏色 (應該要是紅色的警報)
            self.assertEqual(embed.title, "🚨 【盤前財報季雷達預警】")
            self.assertEqual(embed.color, discord.Color.red())
            
            # 檢查內文是否正確包含了標的名稱與倒數天數
            self.assertIn("TSLA", embed.description)
            self.assertIn("NVDA", embed.description)
            self.assertIn("倒數 **2** 天", embed.description)

    async def test_04_user_forbidden_dm(self):
        """測試案例 4：發送私訊時遇到 discord.Forbidden 錯誤 (應被安全捕捉)"""
        fake_port = [(3, 3, "AMZN", "CALL", 100.0, "2026-03-20", 2.0, 1, False)]
        
        mock_resp = MagicMock()
        mock_resp.status = 403
        mock_resp.reason = "Forbidden"
        
        with patch('cogs.trading.database.get_all_portfolio', return_value=fake_port), \
             patch('cogs.trading.database.get_all_watchlist', return_value=[]), \
             patch('cogs.trading.yf.Ticker', return_value=MagicMock()), \
             patch('cogs.trading.market_math.get_next_earnings_date') as mock_earnings:
            
            real_now = datetime.now(ny_tz)
            mock_earnings.return_value = (real_now + timedelta(days=2)).date()
            
            self.mock_user.send.side_effect = discord.Forbidden(mock_resp, "Cannot send message")
            
            try:
                await self.cog.pre_market_risk_monitor.coro(self.cog)
            except discord.Forbidden:
                self.fail("discord.Forbidden 沒有被捕捉處理！")

if __name__ == '__main__':
    unittest.main()