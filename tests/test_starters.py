"""Tests for starters.py -- first-run empty configs.

ensure_starters() writes only missing files, never overwrites, and what it
writes must be accepted by the real readers: load_layout, route_entry,
read_subreddit_map, load_manifest, load_series_aliases, load_shortname_map.

    python -m unittest discover -s tests
"""
import json
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

import archive
import manifest
import shortname
import starters
import tagger

EXPECTED = ["manifest.json", "subreddit_map.json", os.path.join("data", "series_aliases.json"),
            "layout.json", "known_series_names.txt"]


class StartersTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="starters-"))
        self._prev = os.getcwd()
        os.chdir(self.tmp)  # the data root is the cwd

    def tearDown(self):
        os.chdir(self._prev)
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_fresh_root_gets_every_starter(self):
        written = starters.ensure_starters()
        self.assertEqual([str(p) for p in written], EXPECTED)
        for name in EXPECTED:
            self.assertTrue((self.tmp / name).is_file(), name)
        self.assertEqual((self.tmp / "known_series_names.txt").read_text(encoding="utf-8"), "")
        # No temp files left behind.
        self.assertEqual(sorted(p.name for p in self.tmp.rglob(".starter-*")), [])

    def test_second_run_is_a_no_op(self):
        starters.ensure_starters()
        before = {p: p.read_bytes() for p in self.tmp.rglob("*") if p.is_file()}
        self.assertEqual(starters.ensure_starters(), [])
        after = {p: p.read_bytes() for p in self.tmp.rglob("*") if p.is_file()}
        self.assertEqual(before, after)

    def test_never_overwrites_existing_files(self):
        mine = {
            "manifest.json": '{"t3_x": {"status": "pending_review"}}',
            "subreddit_map.json": '{"somesub": {"franchise": "X"}}',
            "layout.json": '{"mine": true, "shortname_file": "codes.txt"}',
            "codes.txt": "AB = Some Series\n",
            os.path.join("data", "series_aliases.json"): '{"aliases": {"a": "b"}}',
        }
        for name, text in mine.items():
            (self.tmp / name).parent.mkdir(parents=True, exist_ok=True)
            (self.tmp / name).write_text(text, encoding="utf-8")
        self.assertEqual(starters.ensure_starters(), [])
        for name, text in mine.items():
            self.assertEqual((self.tmp / name).read_text(encoding="utf-8"), text, name)
        # The layout names its own shortname file, so the generic one is not made.
        self.assertFalse((self.tmp / "known_series_names.txt").exists())

    def test_existing_layouts_missing_relative_shortname_file_is_created(self):
        (self.tmp / "layout.json").write_text('{"shortname_file": "my_codes.txt"}', encoding="utf-8")
        self.assertIn(Path("my_codes.txt"), starters.ensure_starters())
        self.assertEqual((self.tmp / "my_codes.txt").read_text(encoding="utf-8"), "")

    def test_absolute_shortname_file_is_never_created(self):
        elsewhere = Path(tempfile.mkdtemp(prefix="starters-drive-")) / "Unmounted" / "codes.txt"
        (self.tmp / "layout.json").write_text(json.dumps({"shortname_file": str(elsewhere)}),
                                              encoding="utf-8")
        starters.ensure_starters()
        self.assertFalse(elsewhere.exists())
        self.assertFalse(elsewhere.parent.exists())

    def test_starters_are_accepted_by_the_real_readers(self):
        starters.ensure_starters()
        self.assertEqual(manifest.load_manifest(), {})
        self.assertEqual(tagger.read_subreddit_map(), {})
        self.assertEqual(shortname.load_series_aliases(), {})
        layout = shortname.load_layout()
        self.assertEqual(shortname.load_shortname_map(layout), [])
        self.assertEqual(set(layout["special_folders"]), {
            "crossover", "wallpaper_root", "wallpaper_pc", "wallpaper_phone",
            "others_oc", "others_unknown_source", "others_known_series"})
        sn = shortname.load_shortname_map(layout)
        cases = [
            ({"crossover": True, "franchise": ["A", "B"]}, ("move", "Crossover")),
            ({"title": "[original] x", "franchise": [], "character_guess": []},
             ("move", "Others/Artist's Original")),
            ({"franchise": [], "character_guess": []}, ("move", "Others/Unknown Sauce")),
            ({"franchise": ["Some Series"], "character_guess": ["X"]}, ("flag", None)),
        ]
        for entry, (action, dest) in cases:
            result = archive.route_entry(entry, layout, sn, {})
            self.assertEqual((result.action, result.dest_dir), (action, dest), entry)

    def test_starter_layout_matches_the_template_shape(self):
        # Every required key the template documents is present, with its default.
        example = json.loads((REPO / "layout.example.json").read_text(encoding="utf-8"))
        self.assertEqual(starters.STARTER_LAYOUT["special_folders"].keys(),
                         example["special_folders"].keys())
        self.assertEqual(starters.STARTER_LAYOUT["group_subfolder"], example["group_subfolder"])
        # ...but none of the template's invented franchises.
        for key in ("franchises", "franchise_aliases", "character_aliases"):
            self.assertEqual(starters.STARTER_LAYOUT[key], {})

    def test_checkout_is_a_no_op(self):
        # In this repo every starter already exists (the user's real configs), so
        # nothing would be written. Checked by listing, never by writing.
        os.chdir(REPO)
        for path, _ in [(Path("manifest.json"), 0), (Path("subreddit_map.json"), 0),
                        (Path("data") / "series_aliases.json", 0), (Path("layout.json"), 0)]:
            if not path.exists():
                self.skipTest(f"{path} not present in this checkout")
        shortname_path = starters._shortname_path()
        self.assertTrue(shortname_path is None or shortname_path.exists())


if __name__ == "__main__":
    unittest.main(verbosity=2)
