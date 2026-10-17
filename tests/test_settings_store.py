"""Tests for settings.py -- the data/settings.json launch-mode store.

Pure file I/O, no gradio and no review import. The store holds exactly one
decision today: {"window": "browser" | "native"}. Anything unreadable, missing
or out of range reads back as the safe default, "browser".

    python -m unittest discover -s tests
    python tests/test_settings_store.py
"""
import json
import sys
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "src"))

import settings


class WindowModeReadTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="oshiire-settings-"))
        self.path = self.tmp / "settings.json"

    def test_missing_file_defaults_to_browser(self):
        self.assertFalse(self.path.exists())
        self.assertEqual(settings.window_mode(self.path), "browser")

    def test_bad_json_defaults_to_browser(self):
        self.path.write_text("{not valid json", encoding="utf-8")
        self.assertEqual(settings.window_mode(self.path), "browser")

    def test_missing_key_defaults_to_browser(self):
        self.path.write_text(json.dumps({"something_else": 1}), encoding="utf-8")
        self.assertEqual(settings.window_mode(self.path), "browser")

    def test_unknown_value_defaults_to_browser(self):
        self.path.write_text(json.dumps({"window": "fullscreen"}), encoding="utf-8")
        self.assertEqual(settings.window_mode(self.path), "browser")

    def test_non_string_value_defaults_to_browser(self):
        self.path.write_text(json.dumps({"window": 7}), encoding="utf-8")
        self.assertEqual(settings.window_mode(self.path), "browser")

    def test_reads_native_when_set(self):
        self.path.write_text(json.dumps({"window": "native"}), encoding="utf-8")
        self.assertEqual(settings.window_mode(self.path), "native")


class WindowModeWriteTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="oshiire-settings-"))
        self.path = self.tmp / "settings.json"

    def test_round_trip_native(self):
        settings.save_window_mode("native", self.path)
        self.assertEqual(settings.window_mode(self.path), "native")

    def test_round_trip_browser(self):
        settings.save_window_mode("native", self.path)  # move off default first
        settings.save_window_mode("browser", self.path)
        self.assertEqual(settings.window_mode(self.path), "browser")

    def test_written_file_is_the_documented_shape(self):
        settings.save_window_mode("native", self.path)
        data = json.loads(self.path.read_text(encoding="utf-8"))
        self.assertEqual(data, {"window": "native"})

    def test_save_leaves_no_tmp_file_behind(self):
        settings.save_window_mode("native", self.path)
        leftovers = [p.name for p in self.tmp.iterdir() if p.name != "settings.json"]
        self.assertEqual(leftovers, [])

    def test_save_rejects_unknown_value(self):
        with self.assertRaises(ValueError):
            settings.save_window_mode("sideways", self.path)
        self.assertFalse(self.path.exists())


class ThemeReadWriteTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="oshiire-settings-"))
        self.path = self.tmp / "settings.json"

    def test_missing_file_defaults_to_system(self):
        self.assertEqual(settings.theme(self.path), "system")

    def test_missing_key_defaults_to_system(self):
        self.path.write_text(json.dumps({"window": "native"}), encoding="utf-8")
        self.assertEqual(settings.theme(self.path), "system")

    def test_unknown_value_defaults_to_system(self):
        self.path.write_text(json.dumps({"theme": "sepia"}), encoding="utf-8")
        self.assertEqual(settings.theme(self.path), "system")

    def test_round_trip_dark_and_light(self):
        settings.save_theme("dark", self.path)
        self.assertEqual(settings.theme(self.path), "dark")
        settings.save_theme("light", self.path)
        self.assertEqual(settings.theme(self.path), "light")

    def test_theme_and_window_coexist(self):
        settings.save_window_mode("native", self.path)
        settings.save_theme("dark", self.path)
        data = json.loads(self.path.read_text(encoding="utf-8"))
        self.assertEqual(data, {"window": "native", "theme": "dark"})

    def test_save_rejects_unknown_theme(self):
        with self.assertRaises(ValueError):
            settings.save_theme("neon", self.path)


class ThemeParamTest(unittest.TestCase):
    def test_system_has_no_param(self):
        self.assertEqual(settings.theme_param("system"), "")

    def test_light_and_dark(self):
        self.assertEqual(settings.theme_param("light"), "__theme=light")
        self.assertEqual(settings.theme_param("dark"), "__theme=dark")

    def test_unknown_is_treated_as_system(self):
        self.assertEqual(settings.theme_param("sepia"), "")


if __name__ == "__main__":
    unittest.main(verbosity=2)
