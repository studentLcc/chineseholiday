"""Regression tests for per-instance lunar calendar dates."""

from __future__ import annotations

import datetime
from pathlib import Path
import sys
import unittest


COMPONENT_DIR = Path(__file__).resolve().parents[1] / "custom_components" / "chineseholiday"
sys.path.insert(0, str(COMPONENT_DIR))

import lunar  # noqa: E402


class CalendarTodayTests(unittest.TestCase):
    def test_holiday_conversions_do_not_change_sensor_calendar(self) -> None:
        sensor_calendar = lunar.CalendarToday(datetime.datetime(2026, 10, 29))

        self.assertEqual(sensor_calendar.lunar(), (2026, 9, 20))
        self.assertEqual(sensor_calendar.lunar_date_description(), "丙午马年 九月二十")
        self.assertEqual(sensor_calendar.festival_description(), "")

        # These are the lunar dates converted by the holiday fallback logic.
        for month, day in ((1, 1), (5, 5), (8, 15)):
            lunar.CalendarToday.lunar_to_solar(2026, month, day)

        self.assertEqual(sensor_calendar.solar(), (2026, 10, 29))
        self.assertEqual(sensor_calendar.lunar(), (2026, 9, 20))
        self.assertEqual(sensor_calendar.lunar_date_description(), "丙午马年 九月二十")
        self.assertEqual(sensor_calendar.festival_description(), "")

    def test_mid_autumn_still_uses_its_own_lunar_date(self) -> None:
        calendar = lunar.CalendarToday(datetime.datetime(2026, 9, 25))

        self.assertEqual(calendar.lunar(), (2026, 8, 15))
        self.assertEqual(calendar.festival_description(), "中秋节")

    def test_year_specific_festivals_do_not_mutate_other_instances(self) -> None:
        calendar_2026 = lunar.CalendarToday(datetime.datetime(2026, 5, 10))
        calendar_2027 = lunar.CalendarToday(datetime.datetime(2027, 5, 9))

        self.assertIn("母亲节", calendar_2026.festival_description())
        self.assertIn("母亲节", calendar_2027.festival_description())
        self.assertIn("母亲节", calendar_2026.festival_description())

    def test_solar_age_uses_the_sensor_date(self) -> None:
        today = datetime.date(2026, 10, 29)

        self.assertEqual(
            lunar.CalendarToday.get_age_by_birth_solar(2000, 10, 30, today), 25
        )
        self.assertEqual(
            lunar.CalendarToday.get_age_by_birth_solar(2000, 10, 29, today), 26
        )


if __name__ == "__main__":
    unittest.main()
