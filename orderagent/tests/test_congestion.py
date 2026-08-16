import datetime
import unittest

from orderagent import congestion


class CongestionTest(unittest.TestCase):
    def test_weekday_lunch_is_busy(self):
        result = congestion.estimate(datetime.datetime(2026, 8, 17, 12, 15))  # Monday
        self.assertEqual(result["level"], "busy")

    def test_weekday_midafternoon_is_calm(self):
        result = congestion.estimate(datetime.datetime(2026, 8, 17, 15, 0))
        self.assertEqual(result["level"], "calm")

    def test_weekend_dinner_is_busy(self):
        result = congestion.estimate(datetime.datetime(2026, 8, 16, 18, 0))  # Sunday
        self.assertEqual(result["level"], "busy")

    def test_basis_always_declares_estimate(self):
        result = congestion.estimate(datetime.datetime(2026, 8, 16, 3, 0))
        self.assertEqual(result["basis"], "time_pattern_estimate")


if __name__ == "__main__":
    unittest.main()
