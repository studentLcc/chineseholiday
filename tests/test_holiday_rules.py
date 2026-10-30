"""Holiday cache, naming, and statutory fallback rules."""

from __future__ import annotations

import datetime
import sys
import types
import unittest
from pathlib import Path


COMPONENT_DIR = Path(__file__).resolve().parents[1] / "custom_components" / "chineseholiday"


def _load_holiday():
    ha = types.ModuleType("homeassistant")
    util = types.ModuleType("homeassistant.util")
    dt = types.ModuleType("homeassistant.util.dt")
    dt.now = lambda: datetime.datetime(2026, 10, 29, 12, 0, 0)
    dt.as_local = lambda value: value
    sys.modules["homeassistant"] = ha
    sys.modules["homeassistant.util"] = util
    sys.modules["homeassistant.util.dt"] = dt
    pkg = types.ModuleType("chineseholiday")
    pkg.__path__ = [str(COMPONENT_DIR)]
    sys.modules["chineseholiday"] = pkg
    import importlib.util

    lunar_spec = importlib.util.spec_from_file_location(
        "chineseholiday.lunar", COMPONENT_DIR / "lunar.py"
    )
    lunar = importlib.util.module_from_spec(lunar_spec)
    sys.modules["chineseholiday.lunar"] = lunar
    lunar_spec.loader.exec_module(lunar)
    holiday_spec = importlib.util.spec_from_file_location(
        "chineseholiday.holiday", COMPONENT_DIR / "holiday.py"
    )
    holiday = importlib.util.module_from_spec(holiday_spec)
    sys.modules["chineseholiday.holiday"] = holiday
    holiday_spec.loader.exec_module(holiday)
    return holiday


holiday = _load_holiday()


class HolidayRuleTests(unittest.TestCase):
    def setUp(self) -> None:
        self.holiday = holiday.Holiday(cache_path="/tmp/chineseholiday-test-missing.json")
        self.holiday.today = lambda: datetime.datetime(2026, 10, 29)
        self.holiday._holiday_json = {
            "update_time": "2026-10-29",
            "2026": {
                "0216": "2",
                "0217": "2",
                "0218": "2",
                "0219": "2",
                "1001": "2",
            },
            "2027": {},
        }

    def test_consecutive_spring_festival_days_keep_a_real_name(self) -> None:
        names = self.holiday.getHoliday()
        spring = {
            day.isoformat(): name
            for day, name in names.items()
            if day.year == 2026 and day.month == 2
        }
        self.assertEqual(spring["2026-02-16"], "除夕")
        self.assertEqual(spring["2026-02-17"], "春节")
        self.assertEqual(spring["2026-02-18"], "春节")
        self.assertEqual(spring["2026-02-19"], "春节")

    def test_next_holiday_and_details_without_next_year_schedule(self) -> None:
        names = self.holiday.getHoliday()
        future = sorted(day for day in names if day >= datetime.date(2026, 10, 29))
        self.assertEqual(future[0].isoformat(), "2027-01-01")
        self.assertEqual(names[future[0]], "元旦")
        self.assertEqual(self.holiday.nearest_holiday_details(2, 0, 45), [])
        details = self.holiday.nearest_holiday_details(2, 0, None)
        self.assertGreaterEqual(len(details), 1)
        self.assertEqual(details[0]["name"], "元旦")
        self.assertFalse(details[0]["schedule_published"])
        self.assertEqual(details[0]["rows"], [])

    def test_labor_day_fallback_is_one_statutory_day(self) -> None:
        days = self.holiday._known_holiday_dates({2027})
        self.assertEqual(days[datetime.date(2027, 5, 1)], "劳动节")
        self.assertNotIn(datetime.date(2027, 5, 2), days)

    def test_explicit_workday_beats_the_statutory_fallback(self) -> None:
        self.holiday._holiday_json["2027"] = {"0101": "0"}
        self.holiday._known_holiday_cache = {}
        self.assertEqual(
            self.holiday.is_holiday_status(datetime.datetime(2027, 1, 1)),
            0,
        )
        names = self.holiday.getHoliday()
        self.assertNotIn(datetime.date(2027, 1, 1), names)

    def test_weekend_rest_does_not_hide_statutory_holiday_fallback(self) -> None:
        self.holiday._holiday_json["2027"] = {"0101": "1"}
        self.holiday._known_holiday_cache = {}
        self.assertEqual(
            self.holiday.is_holiday_status(datetime.datetime(2027, 1, 1)),
            2,
        )
        self.assertEqual(
            self.holiday.getHoliday()[datetime.date(2027, 1, 1)], "元旦"
        )

    def test_pending_refresh_bypasses_fresh_cache_ttl(self) -> None:
        cache = Path("/tmp/chineseholiday-pending-refresh.json")
        cache.write_text(
            '{"update_time": "2026-10-29", "refresh_pending": true, "2026": {}}',
            encoding="utf-8",
        )
        checker = holiday.Holiday(cache_path=str(cache))
        checker.today = lambda: datetime.datetime(2026, 10, 29)
        snapshot = checker._prepare_refresh(15)
        self.assertTrue(snapshot["should_fetch"])
        self.assertEqual(len(snapshot["months"]), 6)

    def test_bad_cache_time_is_treated_as_expired(self) -> None:
        cache = Path("/tmp/chineseholiday-bad-update-time.json")
        cache.write_text('{"update_time": "not-a-date", "2026": {}}', encoding="utf-8")
        checker = holiday.Holiday(cache_path=str(cache))
        snapshot = checker._prepare_refresh(15)
        self.assertTrue(snapshot["should_fetch"])

    def test_month_refresh_drops_a_corrected_workday(self) -> None:
        year = {"1001": "2", "1002": "2", "1008": "0"}
        self.holiday._replace_month(
            year,
            10,
            {
                "1002": {"type": 2, "week2": 5},
                "1009": {"type": 0, "week2": 6},
                "1010": {"type": 1, "week2": 7},
            },
        )
        self.assertNotIn("1001", year)
        self.assertEqual(year["1002"], "2")
        self.assertNotIn("1008", year)
        self.assertEqual(year["1009"], "0")
        self.assertEqual(year["1010"], "1")

    def test_invalid_month_payload_does_not_erase_cache(self) -> None:
        year = {"1001": "2", "1002": "0"}
        self.holiday._replace_month(year, 10, None)
        self.holiday._replace_month(year, 10, {"1003": {"bad": "x"}})
        self.assertEqual(year, {"1001": "2", "1002": "0"})

    def test_details_load_disk_cache_when_memory_is_empty(self) -> None:
        cache = Path("/tmp/chineseholiday-disk-cache.json")
        cache.write_text(
            '{"update_time": "2026-10-29", "2027": {"0101": "2", "0102": "2"}}',
            encoding="utf-8",
        )
        checker = holiday.Holiday(cache_path=str(cache))
        checker.today = lambda: datetime.datetime(2026, 10, 29)
        details = checker.nearest_holiday_details(1, 0, None)
        self.assertEqual(details[0]["name"], "元旦")
        self.assertTrue(details[0]["schedule_published"])


if __name__ == "__main__":
    unittest.main()
