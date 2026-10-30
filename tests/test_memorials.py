"""Memorial configuration shared by the card and integration."""

from __future__ import annotations

import importlib.util
from pathlib import Path
import unittest

MODULE = Path(__file__).resolve().parents[1] / "custom_components" / "chineseholiday" / "memorials.py"
SPEC = importlib.util.spec_from_file_location("memorials", MODULE)
memorials = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(memorials)


class MemorialConfigTests(unittest.TestCase):
    def test_card_dates_round_trip_through_integration_storage(self) -> None:
        solar, lunar, timers = {}, {}, []
        memorials.apply_memorial(solar, lunar, timers, name="美娟娟生日", calendar="lunar", date="L12-18", mode="yearly_count")
        memorials.apply_memorial(solar, lunar, timers, name="妈妈生日", calendar="solar", date="1970-03-21", mode="yearly_count")
        memorials.apply_memorial(solar, lunar, timers, name="闰月", calendar="lunar", date="LL5-15", mode="yearly")
        exported = {(item["name"], item["date"]) for item in memorials.export_memorials(solar, lunar, timers)}
        self.assertIn(("美娟娟生日", "L12-18"), exported)
        self.assertIn(("妈妈生日", "1970-03-21"), exported)
        self.assertIn(("闰月", "LL5-15"), exported)
        memorials.apply_memorial(solar, lunar, timers, name="美娟娟生日", calendar="lunar", date="L12-18", mode="yearly", operation="delete")
        self.assertNotIn("1218", lunar)


if __name__ == "__main__":
    unittest.main()
