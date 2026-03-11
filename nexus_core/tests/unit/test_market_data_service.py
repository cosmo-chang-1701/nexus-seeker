import asyncio
import unittest
from unittest.mock import patch, MagicMock

import finnhub
from services.market_data_service import _execute_api_call, _limiter

class TestMarketDataServiceBackoff(unittest.IsolatedAsyncioTestCase):
    async def test_execute_api_call_success_first_try(self):
        # 設置一個成功回傳的 mock func
        mock_func = MagicMock(return_value={"c": 150.0})
        
        result = await _execute_api_call(mock_func, "AAPL")
        
        self.assertEqual(result, {"c": 150.0})
        mock_func.assert_called_once_with("AAPL")

    @patch("services.market_data_service.asyncio.sleep")
    async def test_execute_api_call_retries_on_429(self, mock_sleep):
        # 設置 mock_func 前兩次拋出 429 錯誤，第三次成功
        mock_func = MagicMock(side_effect=[
            Exception("FinnhubAPIException(status_code: 429): API limit reached. Please try again later. Remaining Limit: 0"),
            Exception("FinnhubAPIException(status_code: 429): API limit reached. Please try again later. Remaining Limit: 0"),
            {"c": 150.0}
        ])

        result = await _execute_api_call(mock_func, "AAPL")
        
        # 檢查結果是否為最終成功的結果
        self.assertEqual(result, {"c": 150.0})
        # 檢查 mock_func 呼叫次數是否為 3 次
        self.assertEqual(mock_func.call_count, 3)
        # 檢查 sleep 呼叫次數是否為 2 次
        self.assertEqual(mock_sleep.call_count, 2)

    @patch("services.market_data_service.asyncio.sleep")
    @patch("services.market_data_service.logger")
    async def test_execute_api_call_fails_after_max_retries(self, mock_logger, mock_sleep):
        # 設置 mock_func 總是拋出 429 錯誤
        mock_func = MagicMock(side_effect=Exception("FinnhubAPIException(status_code: 429): API limit reached"))
        
        with self.assertRaises(Exception) as context:
            await _execute_api_call(mock_func, "AAPL")
            
        self.assertIn("429", str(context.exception))
        # 檢查 mock_func 被呼叫 4 次 (初次 + 3次重試)
        self.assertEqual(mock_func.call_count, 4)
        # 檢查 sleep 被呼叫 3 次
        self.assertEqual(mock_sleep.call_count, 3)
