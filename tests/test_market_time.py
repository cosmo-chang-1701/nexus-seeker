import unittest
from unittest.mock import MagicMock, patch
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo
import pandas as pd
import sys
import os

# Ensure the project root is in the path
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

import market_time

class TestMarketTime(unittest.TestCase):
    def setUp(self):
        self.ny_tz = ZoneInfo("America/New_York")

    @patch('market_time.datetime')
    @patch('market_time.nyse_calendar')
    def test_get_next_market_target_time(self, mock_calendar, mock_datetime):
        # Mock current time: Monday, 2023-10-23 10:00:00 NY time (Market is Open)
        mock_now = datetime(2023, 10, 23, 10, 0, 0, tzinfo=self.ny_tz)
        mock_datetime.now.return_value = mock_now
        
        # Mock schedule: Open 9:30, Close 16:00
        # UTC times: 
        # 2023-10-23 09:30 NY = 2023-10-23 13:30 UTC
        # 2023-10-23 16:00 NY = 2023-10-23 20:00 UTC
        mock_schedule = pd.DataFrame({
            'market_open': [pd.Timestamp('2023-10-23 13:30:00+0000')],
            'market_close': [pd.Timestamp('2023-10-23 20:00:00+0000')]
        })
        mock_calendar.schedule.return_value = mock_schedule

        # Test case 1: Get next close (reference='close')
        # Since it is 10:00, next close is today at 16:00
        target = market_time.get_next_market_target_time(reference="close")
        expected_target = datetime(2023, 10, 23, 16, 0, 0, tzinfo=self.ny_tz)
        self.assertEqual(target, expected_target)

        # Test case 2: Get next open (reference='open')
        # Since it is 10:00, next open logic iterates through schedule.
        # But wait, logic says: if now < target_ny: return target_ny
        # Open time is 09:30. Now is 10:00. So 09:30 is NOT returned.
        # If schedule only has today, it returns None?
        # Let's add tomorrow to schedule.
        mock_schedule = pd.DataFrame({
            'market_open': [
                pd.Timestamp('2023-10-23 13:30:00+0000'), 
                pd.Timestamp('2023-10-24 13:30:00+0000')
            ],
            'market_close': [
                pd.Timestamp('2023-10-23 20:00:00+0000'),
                pd.Timestamp('2023-10-24 20:00:00+0000')
            ]
        })
        mock_calendar.schedule.return_value = mock_schedule
        
        target = market_time.get_next_market_target_time(reference="open")
        # Should be tomorrow's open: 2023-10-24 09:30 NY
        expected_target = datetime(2023, 10, 24, 9, 30, 0, tzinfo=self.ny_tz)
        self.assertEqual(target, expected_target)

        # Test case 3: Offset (e.g. 5 minutes before close)
        # reference='close', offset_minutes=-5
        # Target: 16:00 - 5 min = 15:55
        target = market_time.get_next_market_target_time(reference="close", offset_minutes=-5)
        expected_target = datetime(2023, 10, 23, 15, 55, 0, tzinfo=self.ny_tz)
        self.assertEqual(target, expected_target)

    @patch('market_time.datetime')
    def test_get_sleep_seconds(self, mock_datetime):
        # Mock current time
        mock_now = datetime(2023, 10, 23, 10, 0, 0, tzinfo=self.ny_tz)
        mock_datetime.now.return_value = mock_now

        # Test 1: Handle None
        self.assertEqual(market_time.get_sleep_seconds(None), 3600)

        # Test 2: Target is 1 hour later
        target_time = mock_now + timedelta(hours=1)
        seconds = market_time.get_sleep_seconds(target_time)
        self.assertEqual(seconds, 3600)

        # Test 3: Target is in the past (negative seconds)
        target_time = mock_now - timedelta(hours=1)
        seconds = market_time.get_sleep_seconds(target_time)
        self.assertEqual(seconds, -3600)

if __name__ == '__main__':
    unittest.main()
