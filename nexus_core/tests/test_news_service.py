import unittest
from unittest.mock import patch, MagicMock
from services.news_service import fetch_recent_news

class TestNewsService(unittest.IsolatedAsyncioTestCase):
    """測試 NewsService 的功能"""

    @patch('services.news_service.yf.Ticker')
    async def test_fetch_recent_news_success(self, mock_ticker):
        """測試成功獲取新聞標題"""
        # 模擬 Ticker 實例及其 news 屬性
        mock_instance = MagicMock()
        mock_instance.news = [
            {'title': 'Market Rally Continues'},
            {'title': 'Tech Stocks Surge'},
            {'title': 'Fed Meeting Update'}
        ]
        mock_ticker.return_value = mock_instance
        
        symbol = 'AAPL'
        result = await fetch_recent_news(symbol)
        
        # 驗證返回結果格式
        expected = "▪️ Market Rally Continues\n▪️ Tech Stocks Surge\n▪️ Fed Meeting Update\n"
        self.assertEqual(result, expected)
        
        # 驗證 yfinance 被正確呼叫
        mock_ticker.assert_called_once_with(symbol)

    @patch('services.news_service.yf.Ticker')
    async def test_fetch_recent_news_empty(self, mock_ticker):
        """測試當無新聞時的回傳"""
        mock_instance = MagicMock()
        mock_instance.news = []
        mock_ticker.return_value = mock_instance
        
        result = await fetch_recent_news('GOOGL')
        
        self.assertEqual(result, "近期無重大新聞。")

    @patch('services.news_service.yf.Ticker')
    async def test_fetch_recent_news_exception(self, mock_ticker):
        """測試發生異常時的錯誤處理"""
        # 模擬拋出異常
        mock_ticker.side_effect = Exception("Connection error")
        
        result = await fetch_recent_news('TSLA')
        
        self.assertEqual(result, "無法獲取近期新聞。")
        # 這裡會觸發 logger.error，但在單元測試中我們主要關注回傳值

    @patch('services.news_service.yf.Ticker')
    async def test_fetch_recent_news_partial_data(self, mock_ticker):
        """測試新聞數據中缺少 title 欄位的情況"""
        mock_instance = MagicMock()
        mock_instance.news = [
            {'title': 'Valid Title'},
            {'link': 'http://example.com'}  # 缺少 title
        ]
        mock_ticker.return_value = mock_instance
        
        result = await fetch_recent_news('MSFT')
        
        expected = "▪️ Valid Title\n▪️ No Title\n"
        self.assertEqual(result, expected)

if __name__ == '__main__':
    unittest.main()
