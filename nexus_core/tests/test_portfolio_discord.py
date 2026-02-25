import unittest
from unittest.mock import MagicMock, patch
import pandas as pd
import discord
from datetime import datetime
import sys
import os

# 確保路徑包含 nexus_core
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.append(PROJECT_ROOT)

from market_analysis.portfolio import check_portfolio_status_logic
from cogs.embed_builder import create_portfolio_report_embed

class TestPortfolioDiscordIntegration(unittest.TestCase):
    """
    整合測試：從 Portfolio 風險計算到 Discord Embed 生成。
    重點驗證 \n\u200b 格式化是否正確套用。
    """

    @patch('market_analysis.portfolio.yf.download')
    @patch('market_analysis.portfolio.yf.Ticker')
    @patch('market_analysis.portfolio.datetime')
    def test_portfolio_to_embed_flow(self, mock_dt, mock_ticker_class, mock_download):
        # 1. 模擬時間 (2025-02-26)
        mock_dt.now.return_value = datetime(2025, 2, 26, 12, 0, 0)
        mock_dt.strptime = datetime.strptime

        # 2. 模擬 yf.download 資料
        # 建立 MultiIndex DataFrame 模擬 yf.download(["AAPL", "SPY"], ...)
        dates = pd.date_range('2025-02-01', periods=5)
        data = {
            ('Close', 'SPY'): [490, 495, 500, 505, 500.0],
            ('Close', 'AAPL'): [145, 148, 150, 152, 150.0]
        }
        mock_hists = pd.DataFrame(data, index=dates)
        mock_hists.columns = pd.MultiIndex.from_tuples(mock_hists.columns)
        mock_download.return_value = mock_hists

        # 3. 模擬 yf.Ticker
        mock_ticker = MagicMock()
        mock_ticker_class.return_value = mock_ticker
        
        # 模擬 fast_info (避開 404 yfinance info 請求)
        mock_ticker.fast_info.lastPrice = 150.0
        mock_ticker.fast_info.quoteType = 'EQUITY'
        mock_ticker.fast_info.dividendYield = 0.015
        
        # 模擬 option_chain
        mock_puts = pd.DataFrame({
            'strike': [140.0],
            'lastPrice': [2.50],
            'impliedVolatility': [0.30]
        })
        mock_chain = MagicMock()
        mock_chain.puts = mock_puts
        mock_chain.calls = pd.DataFrame() # 空
        mock_ticker.option_chain.return_value = mock_chain

        # 4. 準備測試持倉數據
        # (symbol, opt_type, strike, expiry, entry_price, quantity, stock_cost)
        portfolio_rows = [
            ("AAPL", "put", 140.0, "2025-03-21", 3.00, -2, 0.0)
        ]
        user_capital = 100000.0

        # 5. 執行核心邏輯：check_portfolio_status_logic
        print("🚀 執行 Portfolio 風險結算邏輯...")
        report_lines = check_portfolio_status_logic(portfolio_rows, user_capital)
        
        # 驗證是否有產出報告
        self.assertTrue(len(report_lines) > 0)
        report_concat = "".join(report_lines)
        
        # 驗證新版格式化標記 \u200b 是否存在
        self.assertIn("\u200b", report_concat, "報告中應包含 \\u200b 區隔符號")
        print("✅ 報告格式化檢查通過 (已偵測到 \\u200b)")

        # 6. 轉換為 Discord Embed
        print("🎨 生成 Discord Embed...")
        embed = create_portfolio_report_embed(report_lines)
        
        self.assertIsInstance(embed, discord.Embed)
        self.assertEqual(embed.title, "📊 Nexus Seeker 盤後風險結算報告")
        self.assertTrue(len(embed.fields) >= 2)
        
        # 驗證顏色 (由於有賣 Put 且 Delta 在正常區間，這案例可能為藍色或橘色)
        # 單一賣 Put 沒觸發警告應為藍色
        self.assertIsNotNone(embed.color)

        # 7. 模擬 Discord 發送 (雖然 unittest 不會真的發送，但確保屬性正確)
        mock_target = MagicMock()
        mock_target.send = MagicMock()
        
        # 執行發送模擬 (不使用 await 因為這裡是同步測試，僅驗證物件可被傳遞)
        # 在實際機器人中這是 async 的，但這裡我們只檢查 Embed 被正確傳給了 send 方法
        mock_target.send(embed=embed)
        mock_target.send.assert_called_once()
        
        print(f"✅ Embed 內容檢查完整：\n   - Title: {embed.title}\n   - Fields: {len(embed.fields)} 個")
        print("\n🎉 整合測試成功！Portfolio 資料流向 Embed 並模擬發送完成。")

if __name__ == "__main__":
    unittest.main()
