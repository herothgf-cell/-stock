from datetime import datetime
import unittest


class MarketCalendarTests(unittest.TestCase):
    def test_dst_and_early_close_are_not_fixed_korean_five_am(self):
        from stockjournal.market_calendar import session
        self.assertEqual(session('2026-10-30')['close'].isoformat(),'2026-10-30T16:00:00-04:00')
        self.assertEqual(session('2026-11-02')['close'].isoformat(),'2026-11-02T16:00:00-05:00')
        self.assertEqual(session('2026-11-27')['close'].hour,13)
        self.assertEqual(session('2026-11-27')['after_close'].hour,17)

    def test_holidays_weekends_and_unknown_year_do_not_open(self):
        from stockjournal.market_calendar import session
        self.assertIsNone(session('2026-11-26'))
        self.assertIsNone(session('2026-10-31'))
        with self.assertRaises(ValueError):session('2029-01-02')

    def test_latest_completed_accounts_for_close_buffer_and_holiday(self):
        from stockjournal.market_calendar import latest_completed
        self.assertEqual(latest_completed(datetime.fromisoformat('2026-11-27T13:05:00-05:00')),'2026-11-25')
        self.assertEqual(latest_completed(datetime.fromisoformat('2026-11-27T13:10:00-05:00')),'2026-11-27')
