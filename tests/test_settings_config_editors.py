"""Tests for the remaining Settings config editors.

Two layers:

  * Backend (this file's first two TestCases) -- the pure shortname.py helpers
    the panels write through: save/remove_franchise_alias (0a) and the
    save_series_aliases raise-on-malformed guard (0b). No review.py import.
  * Panels (SettingsPanelTest) -- review.py's Franchise/Character/Series/
    Shortname panel handlers, driven against a SYNTHETIC layout/series/shortname
    tree in a temp cwd, exactly like test_render_contract.py. The real
    gitignored stores are never read or written.

    python -m unittest discover -s tests
    python tests/test_settings_config_editors.py
"""
import json
import os
import shutil
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

import shortname  # noqa: E402


# ---------------------------------------------------------------------------
# Part 0a -- franchise-alias backend.
# ---------------------------------------------------------------------------
class FranchiseAliasBackendTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="oshiire-falias-"))
        self.path = self.tmp / "layout.json"

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _load(self):
        return json.loads(self.path.read_text(encoding="utf-8"))

    def test_save_writes_variant_to_folder(self):
        layout = {}
        shortname.save_franchise_alias("Azure Lane", "Azur Lane", layout, path=self.path)
        self.assertEqual(layout["franchise_aliases"]["Azure Lane"], "Azur Lane")
        self.assertEqual(self._load()["franchise_aliases"]["Azure Lane"], "Azur Lane")

    def test_save_strips_both_sides(self):
        layout = {}
        shortname.save_franchise_alias("  Variant  ", "  Folder  ", layout, path=self.path)
        self.assertEqual(layout["franchise_aliases"]["Variant"], "Folder")

    def test_save_null_folder_writes_json_null(self):
        layout = {}
        shortname.save_franchise_alias("Re:Zero", None, layout, path=self.path)
        self.assertIsNone(layout["franchise_aliases"]["Re:Zero"])
        # the SHAPE that means "known franchise, no folder yet" -- JSON null,
        # not the string "None" or "".
        self.assertIn('"Re:Zero": null', self.path.read_text(encoding="utf-8"))

    def test_remove_is_normalize_matched_and_drops_every_casing(self):
        layout = {"franchise_aliases": {"azurelane": "Azur Lane",
                                        "AzureLane": None,
                                        "Genshin": "Genshin Impact"}}
        shortname.save_layout(layout, self.path)
        shortname.remove_franchise_alias("AZURELANE", layout, path=self.path)
        self.assertNotIn("azurelane", layout["franchise_aliases"])
        self.assertNotIn("AzureLane", layout["franchise_aliases"])
        self.assertIn("Genshin", layout["franchise_aliases"], "an unrelated key must survive")
        self.assertEqual(self._load()["franchise_aliases"], {"Genshin": "Genshin Impact"})

    def test_remove_absent_key_is_a_noop_and_writes_nothing(self):
        layout = {"franchise_aliases": {"X": "Y"}}
        shortname.remove_franchise_alias("not there", layout, path=self.path)
        self.assertFalse(self.path.exists(), "a no-op remove must not create the file")
        self.assertEqual(layout["franchise_aliases"], {"X": "Y"})

    def test_remove_missing_table_is_a_noop(self):
        layout = {}
        shortname.remove_franchise_alias("anything", layout, path=self.path)
        self.assertFalse(self.path.exists())
        self.assertEqual(layout, {})

    def test_remove_prunes_an_emptied_table(self):
        layout = {"franchise_aliases": {"Only": "One Folder"}}
        shortname.save_layout(layout, self.path)
        shortname.remove_franchise_alias("Only", layout, path=self.path)
        self.assertNotIn("franchise_aliases", layout)
        self.assertNotIn("franchise_aliases", self._load())


# ---------------------------------------------------------------------------
# Part 0b -- save_series_aliases refuses a malformed file rather than flattening.
# ---------------------------------------------------------------------------
class SeriesAliasesGuardTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="oshiire-seriesguard-"))
        self.path = self.tmp / "series_aliases.json"

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_non_object_top_level_raises_and_does_not_overwrite(self):
        self.path.write_text(json.dumps(["not", "an", "object"]), encoding="utf-8")
        with self.assertRaises(ValueError):
            shortname.save_series_aliases({"a": "b"}, path=self.path)
        self.assertEqual(json.loads(self.path.read_text(encoding="utf-8")),
                         ["not", "an", "object"], "the malformed file must be left intact")

    def test_normal_round_trip_preserves_top_level_siblings(self):
        self.path.write_text(
            json.dumps({"_comment": "keep me", "aliases": {"a": "b"}}), encoding="utf-8"
        )
        shortname.save_series_aliases({"c": "d"}, path=self.path)
        after = json.loads(self.path.read_text(encoding="utf-8"))
        self.assertEqual(after["_comment"], "keep me")
        self.assertEqual(after["aliases"], {"c": "d"})


# ---------------------------------------------------------------------------
# Parts 1/2 -- the panel handlers, driven against a synthetic tree in a temp
# cwd. review.py is imported once, bound to this sandbox; each test snapshots
# and restores the in-memory globals AND the on-disk stores it touches.
# ---------------------------------------------------------------------------
PANEL_LAYOUT = {
    "archive_dir_env": "ARCHIVE_DIR",
    "group_subfolder": "Others_Group",
    "special_folders": {
        "crossover": "Crossover",
        "wallpaper_root": "Wallpaper", "wallpaper_pc": "PC", "wallpaper_phone": "Phone",
        "others_oc": "Others/Artist's Original",
        "others_unknown_source": "Others/Unknown Sauce",
        "others_known_series": "Others/Known Series",
    },
    "shortname_file": "known_series_names.txt",
    "franchise_aliases": {"Azure Lane": "Azur Lane"},
    "character_aliases": {"Genshin Impact": {"Raiden Shogun": "Raiden"}},
    "franchises": {
        "Genshin Impact": {"style": "nested", "characters": ["Raiden", "Hu Tao"]},
        "Azur Lane": {"style": "flat"},
    },
}

SHORTNAME_TEXT = "# Known series shortnames\nNK = NIKKE The Goddess of Victory\n"
SERIES_ALIASES = {"_comment": "hand-written note", "aliases": {"Re Zero": "Re:Zero"}}


def _build_panel_sandbox(tmp: Path) -> None:
    (tmp / "layout.json").write_text(json.dumps(PANEL_LAYOUT, indent=2), encoding="utf-8")
    (tmp / "known_series_names.txt").write_text(SHORTNAME_TEXT, encoding="utf-8")
    (tmp / "data").mkdir()
    (tmp / "data" / "series_aliases.json").write_text(
        json.dumps(SERIES_ALIASES, indent=2), encoding="utf-8"
    )
    shutil.copy(REPO / "subreddit_map.example.json", tmp / "subreddit_map.json")
    (tmp / "staging").mkdir()
    (tmp / "manifest.json").write_text("{}", encoding="utf-8")


class SettingsPanelTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._tmp = tempfile.mkdtemp(prefix="oshiire-panels-")
        cls._prev_cwd = os.getcwd()
        _build_panel_sandbox(Path(cls._tmp))
        os.chdir(cls._tmp)
        cls._prev_archive = os.environ.pop("ARCHIVE_DIR", None)
        with redirect_stdout(StringIO()):
            import review
        cls.review = review

    @classmethod
    def tearDownClass(cls):
        os.chdir(cls._prev_cwd)
        if cls._prev_archive is not None:
            os.environ["ARCHIVE_DIR"] = cls._prev_archive
        shutil.rmtree(cls._tmp, ignore_errors=True)

    def setUp(self):
        # `review` is a module singleton: under `discover`, an earlier test file
        # imports it first and binds its globals to a different sandbox. So each
        # test rewrites the pristine store files AND forces review's in-memory
        # globals to match, making these tests independent of import order.
        import copy
        review = self.review
        self._layout_snap = copy.deepcopy(review.layout)
        self._series_snap = review.series_aliases
        self._shortname_snap = review.shortname_entries

        Path("layout.json").write_text(json.dumps(PANEL_LAYOUT, indent=2), encoding="utf-8")
        Path("data/series_aliases.json").write_text(json.dumps(SERIES_ALIASES, indent=2), encoding="utf-8")
        Path("known_series_names.txt").write_text(SHORTNAME_TEXT, encoding="utf-8")

        # Handlers mutate `layout` in place, so replace its CONTENTS, not the
        # binding other module code holds.
        review.layout.clear()
        review.layout.update(copy.deepcopy(PANEL_LAYOUT))
        review.series_aliases = review.load_series_aliases()
        review.shortname_entries = review.load_shortname_map(review.layout)

    def tearDown(self):
        review = self.review
        review.layout.clear()
        review.layout.update(self._layout_snap)
        review.series_aliases = self._series_snap
        review.shortname_entries = self._shortname_snap

    def _call(self, fn, *args):
        with redirect_stdout(StringIO()):
            return fn(*args)

    def _layout_file(self):
        return json.loads(Path("layout.json").read_text(encoding="utf-8"))

    def _series_file(self):
        return json.loads(Path("data/series_aliases.json").read_text(encoding="utf-8"))

    # --- Panel A: franchise aliases ----------------------------------------

    def test_franchise_alias_save_and_delete_round_trip(self):
        review = self.review
        self._call(review.on_fa_save, "Blue Archive", "Blue Archive", False, None)
        self.assertEqual(self._layout_file()["franchise_aliases"]["Blue Archive"], "Blue Archive")
        self._call(review.on_fa_delete, "Blue Archive")
        self.assertNotIn("Blue Archive", self._layout_file()["franchise_aliases"])
        # an untouched sibling survives both writes
        self.assertEqual(self._layout_file()["franchise_aliases"]["Azure Lane"], "Azur Lane")

    def test_franchise_alias_null_writes_json_null(self):
        review = self.review
        self._call(review.on_fa_save, "No Folder Franchise", "", True, None)
        self.assertIsNone(self._layout_file()["franchise_aliases"]["No Folder Franchise"])

    # --- Panel B: character aliases ----------------------------------------

    def test_character_alias_franchise_name_key_is_blocked_and_writes_nothing(self):
        review = self.review
        before = self._layout_file()["character_aliases"]["Genshin Impact"]
        render = self._call(review.on_ca_save, "Genshin Impact", "Genshin Impact", "Raiden", None)
        # nothing was written -- the guarded shape never reaches disk
        after = self._layout_file()["character_aliases"]["Genshin Impact"]
        self.assertEqual(after, before)
        self.assertNotIn("Genshin Impact", after)
        self.assertIn("own name", render[review.ca_status_md])

    def test_character_alias_save_and_delete_round_trip(self):
        review = self.review
        self._call(review.on_ca_save, "Genshin Impact", "Ei", "Raiden", None)
        self.assertEqual(self._layout_file()["character_aliases"]["Genshin Impact"]["Ei"], "Raiden")
        self._call(review.on_ca_delete, "Genshin Impact", "Ei")
        self.assertNotIn("Ei", self._layout_file()["character_aliases"]["Genshin Impact"])
        # the hand-written alias is untouched
        self.assertEqual(self._layout_file()["character_aliases"]["Genshin Impact"]["Raiden Shogun"], "Raiden")

    # --- Panel C: series aliases -------------------------------------------

    def test_series_alias_save_and_delete_round_trip(self):
        review = self.review
        self._call(review.on_sa_save, "FGO", "Fate/Grand Order", None)
        self.assertEqual(self._series_file()["aliases"]["FGO"], "Fate/Grand Order")
        # the hand-written _comment envelope survives the write (Part 0b path)
        self.assertEqual(self._series_file()["_comment"], "hand-written note")
        self._call(review.on_sa_delete, "FGO")
        self.assertNotIn("FGO", self._series_file()["aliases"])
        self.assertEqual(self._series_file()["_comment"], "hand-written note")

    # --- Panel D: shortnames -----------------------------------------------

    def test_shortname_save_and_delete_round_trip(self):
        review = self.review
        self._call(review.on_sn_save, "HSR", "Honkai Star Rail", None)
        text = Path("known_series_names.txt").read_text(encoding="utf-8")
        self.assertIn("HSR = Honkai Star Rail", text)
        # the seeded comment and entry are preserved by the line-based writer
        self.assertIn("# Known series shortnames", text)
        self.assertIn("NK = NIKKE The Goddess of Victory", text)
        self._call(review.on_sn_delete, "Honkai Star Rail")
        self.assertNotIn("Honkai Star Rail", Path("known_series_names.txt").read_text(encoding="utf-8"))

    def test_shortname_code_collision_is_blocked(self):
        review = self.review
        render = self._call(review.on_sn_save, "NK", "A Totally Different Series", None)
        text = Path("known_series_names.txt").read_text(encoding="utf-8")
        self.assertNotIn("A Totally Different Series", text)
        self.assertIn("NIKKE The Goddess of Victory", text)
        self.assertIn("already used", render[review.sn_status_md])


if __name__ == "__main__":
    unittest.main(verbosity=2)
