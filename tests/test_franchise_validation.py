"""Tests for review._franchise_validation_md -- the inline, read-only advisory
that flags typed FRANCHISE names not resolving, with a difflib "did you mean"
suggestion. The franchise-level sibling of _character_validation_md.

The verdict is shortname.resolve_franchise (the same call archive.py flags
with), so a franchise is KNOWN whenever status != "unmapped" -- including an
alias to null ("known, no folder yet"), which must never be flagged. difflib is
only the suggestion and never changes the verdict. Unlike the character check
the helper is ungated: every typed franchise is independently checkable, so a
two-franchise list flags only the name that doesn't resolve.

Runs entirely in a temp cwd against a SYNTHETIC layout.json (franchises map with
"Pokemon", a normal franchise_aliases entry, and a null-valued one) plus a
data/series_aliases.json, so it never touches the user's real config.

    python -m unittest discover -s tests
    python tests/test_franchise_validation.py
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

# "Pokemon" and "Genshin Impact" as franchise folders (resolve by identity),
# "Azure Lane" -> "Azur Lane" as a normal alias (resolves by alias), and
# "Re:Zero" -> null as a "known, no folder yet" alias (must NOT be flagged).
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
    "franchise_aliases": {"Azure Lane": "Azur Lane", "Re:Zero": None},
    "character_aliases": {},
    "franchises": {
        "Pokemon": {"style": "nested", "characters": ["Pikachu"]},
        "Genshin Impact": {"style": "nested", "characters": ["Ei"]},
    },
}

# A canonical-name mapping whose variant string ("Pocket Monsters") is nowhere
# near "Pokemon" by difflib, so a silent verdict can only come from
# resolve_franchise canonicalizing it -- never from the suggestion matcher.
SERIES_ALIASES = {"aliases": {"Pocket Monsters": "Pokemon"}}


def _build_sandbox(tmp: Path) -> None:
    (tmp / "layout.json").write_text(json.dumps(SYNTHETIC_LAYOUT, indent=2), encoding="utf-8")
    (tmp / "data").mkdir()
    (tmp / "data" / "series_aliases.json").write_text(
        json.dumps(SERIES_ALIASES, indent=2), encoding="utf-8"
    )
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
            "franchise": ["Pokemon"],
            "character_guess": ["Pikachu"],
            "guess_confidence": "medium",
            "guess_source": "title",
            "crossover": False,
        }
    }
    (tmp / "manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")


class FranchiseValidationTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._tmp = tempfile.mkdtemp(prefix="oshiire-franchvalid-")
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
        review.series_aliases = SERIES_ALIASES["aliases"]

    @classmethod
    def tearDownClass(cls):
        cls.review.layout = cls._prev_layout
        cls.review.series_aliases = cls._prev_series_aliases
        os.chdir(cls._prev_cwd)
        if cls._prev_archive is not None:
            os.environ["ARCHIVE_DIR"] = cls._prev_archive
        shutil.rmtree(cls._tmp, ignore_errors=True)

    def _md(self, franchises):
        with redirect_stdout(StringIO()):
            return self.review._franchise_validation_md(franchises)

    # --- silent paths -------------------------------------------------------

    def test_silent_empty_list(self):
        self.assertEqual(self._md([]), "")

    def test_silent_resolves_by_identity(self):
        # "Pokemon" is a franchises key -> status "identity" -> silent.
        self.assertEqual(self._md(["Pokemon"]), "")

    def test_silent_resolves_by_alias(self):
        # "Azure Lane" -> "Azur Lane" via franchise_aliases -> status "aliased".
        self.assertEqual(self._md(["Azure Lane"]), "")

    def test_silent_null_alias_known_no_folder_yet(self):
        # "Re:Zero" -> null: an explicit alias to no-folder-yet. status is
        # "aliased", not "unmapped", so it must NOT be flagged.
        self.assertEqual(self._md(["Re:Zero"]), "")

    def test_silent_series_alias_canonicalization(self):
        # "Pocket Monsters" resolves ONLY because resolve_franchise
        # canonicalizes it to "Pokemon" first -- difflib would never match the
        # two strings, so silence proves the verdict is resolve_franchise.
        self.assertEqual(self._md(["Pocket Monsters"]), "")

    # --- fires --------------------------------------------------------------

    def test_near_miss_suggests_closest_franchise(self):
        md = self._md(["pokeon"])
        self.assertIn("Doesn't match a known franchise", md)
        self.assertIn("**pokeon** — did you mean **Pokemon**?", md)

    def test_no_close_match_says_no_matching_franchise(self):
        md = self._md(["Zzzzzz"])
        self.assertIn("**Zzzzzz** — no matching franchise.", md)
        self.assertNotIn("did you mean", md)

    # --- validates each name (unlike the character check) -------------------

    def test_two_franchise_list_flags_only_the_unresolved(self):
        md = self._md(["Pokemon", "Zzzzzz"])
        bullets = [ln for ln in md.splitlines() if ln.startswith("- ")]
        self.assertEqual(len(bullets), 1, md)
        self.assertIn("**Zzzzzz** — no matching franchise.", md)
        self.assertNotIn("Pokemon", "\n".join(bullets))


if __name__ == "__main__":
    unittest.main(verbosity=2)
