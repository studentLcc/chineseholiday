"""Regression tests for Home Assistant translation files."""

import json
from pathlib import Path
import unittest


COMPONENT_DIR = Path(__file__).resolve().parents[1] / "custom_components" / "chineseholiday"


class TranslationStructureTests(unittest.TestCase):
    """Translation leaves must be strings accepted by Home Assistant."""

    def test_translation_leaves_are_strings(self) -> None:
        files = [COMPONENT_DIR / "strings.json"]
        files.extend(sorted((COMPONENT_DIR / "translations").glob("*.json")))

        for path in files:
            with self.subTest(path=path.name):
                translations = json.loads(path.read_text(encoding="utf-8"))
                self.assert_string_leaves(translations, path.name)

    def assert_string_leaves(self, value, path: str) -> None:
        if isinstance(value, dict):
            for key, child in value.items():
                self.assert_string_leaves(child, f"{path}.{key}")
        elif isinstance(value, list):
            for index, child in enumerate(value):
                self.assert_string_leaves(child, f"{path}[{index}]")
        else:
            self.assertIsInstance(value, str, f"{path} must be a string")


if __name__ == "__main__":
    unittest.main()
