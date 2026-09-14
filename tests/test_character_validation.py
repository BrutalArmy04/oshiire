"""Tests for review._character_validation_md -- the inline, read-only advisory
that flags typed character names not resolving to a folder in the tagged
franchise, with a difflib "did you mean" suggestion.

The helper is a sibling of _character_alias_candidate and MUST agree with it on
when a name is expected to resolve, so these tests pin the shared gating:
silent unless exactly one nested franchise with a non-empty roster and none of
crossover/OC/known-series/same-series-group set; persistently-answered names
(group-routed / dismissed) skipped; the verdict is shortname.resolve_character
(so spacing/order/alias variants that resolve stay silent), and only the
suggestion is difflib.

Runs entirely in a temp cwd against a SYNTHETIC layout.json (a known nested
franchise whose roster holds a one-token name), so it never depends on the
example file's rosters and never touches the user's real config.

    python -m unittest discover -s tests
    python tests/test_character_validation.py
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

# A nested franchise "TestNest" whose roster holds one-token "Shinobu" and a
# two-token "Kamisato Ayaka" (to exercise name-order tolerance), plus an alias
# and a persistent group-route/dismissal, and non-nested / empty-roster
# franchises for the silent-path cases.
SYNTHETIC_LAYOUT = {
    "archive_dir_env": "ARCHIVE_DIR",
    "group_subfolder": "Others_Group",
    "special_folders": {
        "crossover": "Crossover",
        "wallpaper_root": "Wallpaper",
        "wallpaper_pc": "PC",
        "wallpaper_phone": "Phone",
        "others_oc": "Others/Artist's Original",
        "others_unknown_source": "Others/Unknown Sauce",
        "others_known_series": "Others/Known Series",
    },
    "shortname_file": "known_series_names.txt",
    "franchise_aliases": {},
    "character_aliases": {"TestNest": {"Raiden Shogun": "Ei"}},
    "character_group_route": {"TestNest": ["The Crowd"]},
    "character_alias_dismissed": {"TestNest": ["Random Cameo"]},
    "franchises": {
        "TestNest": {"style": "nested", "characters": ["Shinobu", "Ei", "Kamisato Ayaka"]},
        "TestFlat": {"style": "flat"},
        "TestShort": {"style": "shortname"},
        "TestEmpty": {"style": "nested", "characters": []},
    },
}

# _character_validation_md never exercises series-alias canonicalization and the
# sandbox writes no data/series_aliases.json, so the synthetic aliases are empty
# -- but they're pinned like the layout (see setUpClass) so the test never reads
# another test's review.series_aliases under `unittest discover`.
SERIES_ALIASES = {}


def _build_sandbox(tmp: Path) -> None:
    (tmp / "layout.json").write_text(json.dumps(SYNTHETIC_LAYOUT, indent=2), encoding="utf-8")
    shutil.copy(REPO / "known_series_names.example.txt", tmp / "known_series_names.txt")
    # tagger.subreddit_is_mapped exits the process if this is absent; review.py
    # imports tagger at module load.
    shutil.copy(REPO / "subreddit_map.example.json", tmp / "subreddit_map.json")

    staging = tmp / "staging"
    staging.mkdir()
    image_path = staging / "t3_test01.jpg"
    from PIL import Image
    Image.new("RGB", (1920, 1080), (90, 110, 130)).save(image_path)

    manifest = {
        "t3_test01": {
            "post_id": "t3_test01",
            "title": "Test",
            "subreddit": "TestSub",
            "permalink": "https://reddit.com/r/TestSub/comments/test01/",
            "image_url": "https://i.redd.it/test01.jpg",
            "local_path": str(image_path).replace("\\", "/"),
            "fetched_at": "2026-01-01T00:00:00+00:00",
            "status": "pending_review",
            "franchise": ["TestNest"],
            "character_guess": ["Shinobu"],
            "guess_confidence": "medium",
            "guess_source": "title",
            "crossover": False,
        }
    }
    (tmp / "manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")


class CharacterValidationTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._tmp = tempfile.mkdtemp(prefix="oshiire-charvalid-")
        cls._prev_cwd = os.getcwd()
        tmp = Path(cls._tmp)
        _build_sandbox(tmp)
        os.chdir(tmp)
        cls._prev_archive = os.environ.pop("ARCHIVE_DIR", None)
        with redirect_stdout(StringIO()):
            import review
        cls.review = review
        # review.layout / review.series_aliases are module globals bound at
        # first import (from that importer's cwd). Under `unittest discover`
        # review may already be imported by another test, so pin them to THIS
        # sandbox and restore in tearDown -- the helper reads these globals, so
        # the test must be independent of which test imported review first.
        cls._prev_layout = review.layout
        cls._prev_series_aliases = review.series_aliases
        review.layout = SYNTHETIC_LAYOUT
        review.series_aliases = SERIES_ALIASES

    @classmethod
    def tearDownClass(cls):
        cls.review.layout = cls._prev_layout
        cls.review.series_aliases = cls._prev_series_aliases
        os.chdir(cls._prev_cwd)
        if cls._prev_archive is not None:
            os.environ["ARCHIVE_DIR"] = cls._prev_archive
        shutil.rmtree(cls._tmp, ignore_errors=True)

    def _md(self, characters, franchises, **kwargs):
        with redirect_stdout(StringIO()):
            return self.review._character_validation_md(characters, franchises, **kwargs)

    # --- silent paths -------------------------------------------------------

    def test_silent_no_characters(self):
        self.assertEqual(self._md([], ["TestNest"]), "")

    def test_silent_no_franchise(self):
        self.assertEqual(self._md(["Zzzzzz"], []), "")

    def test_silent_multi_franchise(self):
        # A name that would flag against TestNest is silent when the roster is
        # ambiguous across two franchises.
        self.assertEqual(self._md(["Zzzzzz"], ["TestNest", "TestFlat"]), "")

    def test_silent_crossover(self):
        self.assertEqual(self._md(["Zzzzzz"], ["TestNest"], crossover=True), "")

    def test_silent_oc(self):
        self.assertEqual(self._md(["Zzzzzz"], ["TestNest"], is_oc=True), "")

    def test_silent_known_series(self):
        self.assertEqual(self._md(["Zzzzzz"], ["TestNest"], known_series=True), "")

    def test_silent_same_series_group(self):
        self.assertEqual(self._md(["Zzzzzz"], ["TestNest"], same_series_group=True), "")

    def test_silent_flat_franchise(self):
        self.assertEqual(self._md(["Zzzzzz"], ["TestFlat"]), "")

    def test_silent_shortname_franchise(self):
        self.assertEqual(self._md(["Zzzzzz"], ["TestShort"]), "")

    def test_silent_empty_roster(self):
        self.assertEqual(self._md(["Zzzzzz"], ["TestEmpty"]), "")

    # --- the verdict IS resolve_character -----------------------------------

    def test_silent_exact_roster_match(self):
        self.assertEqual(self._md(["Shinobu"], ["TestNest"]), "")

    def test_silent_two_token_reversal_resolves(self):
        # roster has "Kamisato Ayaka"; the reversed order still resolves, so no
        # advisory -- proving the verdict is resolve_character, not a re-matcher.
        self.assertEqual(self._md(["Ayaka Kamisato"], ["TestNest"]), "")

    def test_silent_alias_hit_resolves(self):
        # character_aliases maps "Raiden Shogun" -> "Ei"; resolves, so silent.
        self.assertEqual(self._md(["Raiden Shogun"], ["TestNest"]), "")

    # --- fires --------------------------------------------------------------

    def test_near_miss_suggests_closest_folder(self):
        md = self._md(["Kuki Shinobu"], ["TestNest"])
        self.assertIn("Doesn't match a character folder in TestNest", md)
        self.assertIn("**Kuki Shinobu** — did you mean **Shinobu**?", md)

    def test_no_close_match_says_no_matching_folder(self):
        md = self._md(["Zzzzzz"], ["TestNest"])
        self.assertIn("**Zzzzzz** — no matching folder.", md)
        self.assertNotIn("did you mean", md)

    # --- persistently-answered names are skipped ----------------------------

    def test_group_routed_name_skipped(self):
        # "The Crowd" doesn't resolve but is pinned to Others_Group, so no
        # advisory -- exactly as the alias prompt skips it.
        self.assertEqual(self._md(["The Crowd"], ["TestNest"]), "")

    def test_alias_dismissed_name_skipped(self):
        self.assertEqual(self._md(["Random Cameo"], ["TestNest"]), "")

    # --- multiple names -----------------------------------------------------

    def test_multiple_unresolved_names_each_get_a_bullet(self):
        md = self._md(["Kuki Shinobu", "Zzzzzz"], ["TestNest"])
        bullets = [ln for ln in md.splitlines() if ln.startswith("- ")]
        self.assertEqual(len(bullets), 2, md)
        self.assertIn("**Kuki Shinobu** — did you mean **Shinobu**?", md)
        self.assertIn("**Zzzzzz** — no matching folder.", md)

    def test_resolved_and_unresolved_mix_lists_only_the_unresolved(self):
        # "Shinobu" resolves and must not appear; only "Zzzzzz" is listed.
        md = self._md(["Shinobu", "Zzzzzz"], ["TestNest"])
        bullets = [ln for ln in md.splitlines() if ln.startswith("- ")]
        self.assertEqual(len(bullets), 1, md)
        self.assertIn("**Zzzzzz** — no matching folder.", md)


if __name__ == "__main__":
    unittest.main(verbosity=2)
