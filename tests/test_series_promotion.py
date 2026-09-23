"""Tests for the series-folder writers in shortname.py:

    _validate_new_series_folder   -- can this be a NEW flat franchise folder?
    create_series                 -- add a brand-new flat folder
    _apply_series_promotion       -- pure core: hook shortname tags onto a folder
    preview_series_promotion      -- dry-run on a deep copy, never mutates
    promote_series                -- apply to the real layout, save iff changed

The feature "promote a series out of Others/Known Series (or create one fresh)"
is layout.json ONLY: it never writes the shortname file (the `_CODE` legend
stays as the permanent decoder for already-filed images) and never moves a file
on disk. What these pin down, in the order the bugs would bite:

  1. Promotion routes the tag. The point is stated as a DESTINATION via
     archive.route_entry, not as dict contents: a tag that used to fall through
     to Others/Known Series now files into the new folder. The alias is
     load-bearing -- match_shortname reaches a shortname entry from a leading
     token of the full name, but resolve_franchise does not, so without the
     alias the tag would keep going to Known Series.
  2. The alias KEY is the CANONICALIZED tag. resolve_franchise canonicalizes a
     tag through the series-alias store BEFORE reading franchise_aliases, so a
     raw-tag key that differed from its canonical form would never be read.
  3. An explicit existing alias is never overwritten -- it is reported as
     skipped. An alias the user wrote outranks inferred routing.
  4. Nothing is written when nothing changed, and a refusal (bad name, non-flat
     target) leaves the file byte-for-byte untouched. Asserted on bytes AND
     st_mtime_ns, like the promote/merge tests, because layout.json is
     hand-curated and Drive-synced.
  5. The shortname file is never touched by any of these.

Runs on a synthetic layout in a temp dir with an explicit path= on every write.
Neither the real layout.json nor ARCHIVE_DIR is read or written.

    python -m unittest discover -s tests
    python tests/test_series_promotion.py
"""
import copy
import json
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

import archive  # noqa: E402
from shortname import (  # noqa: E402
    _apply_series_promotion,
    _validate_new_series_folder,
    create_series,
    load_layout,
    load_shortname_map,
    preview_series_promotion,
    promote_series,
)

GENSHIN = "Genshin Impact"
AZUR = "Azur Lane"

BASE_LAYOUT = {
    "group_subfolder": "Others_Group",
    "special_folders": {
        "crossover": "Crossover",
        "wallpaper_root": "Wallpaper",
        "others_oc": "Others/Artist's Original",
        "others_unknown_source": "Others/Unknown Sauce",
        "others_known_series": "Others/Known Series",
    },
    "shortname_file": "known_series_names.txt",
    "franchise_aliases": {},
    "franchises": {
        GENSHIN: {"style": "nested", "characters": ["Hu Tao"]},
        AZUR: {"style": "flat"},
    },
}

# A shortname file the fixture writes: NIKKE reachable both by code and by the
# leading token of its full name.
SHORTNAME_TEXT = "# Known series\nNK = NIKKE The Goddess of Victory\n"


def _entry(franchise, **extra):
    entry = {
        "post_id": "t3_test",
        "title": "Test",
        "franchise": [franchise],
        "character_guess": [],
        "crossover": False,
    }
    entry.update(extra)
    return entry


class SeriesFixture(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="oshiire-series-"))
        self.path = self.tmp / "layout.json"
        self.sn_path = self.tmp / "known_series_names.txt"
        self.layout = copy.deepcopy(BASE_LAYOUT)
        self.layout["shortname_file"] = str(self.sn_path)
        self.path.write_text(json.dumps(self.layout, indent=2), encoding="utf-8")
        self.sn_path.write_text(SHORTNAME_TEXT, encoding="utf-8")
        self.sn_before = self.sn_path.read_bytes()

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _on_disk(self):
        return load_layout(self.path)

    def _raw(self):
        return self.path.read_bytes()

    def _stamp(self):
        return self._raw(), self.path.stat().st_mtime_ns

    def _shortname_entries(self):
        return load_shortname_map(self.layout)

    def _route(self, entry, layout=None):
        return archive.route_entry(
            entry, layout or self.layout, self._shortname_entries(), None
        )

    def assertShortnameUntouched(self):
        self.assertEqual(self.sn_path.read_bytes(), self.sn_before,
                         "the shortname file must never be written by this feature")


class ValidateNewSeriesFolderTest(SeriesFixture):
    def test_returns_the_stripped_name(self):
        self.assertEqual(_validate_new_series_folder(self.layout, "  NIKKE  "), "NIKKE")

    def test_rejects_empty(self):
        for blank in ("", "   "):
            with self.subTest(blank=blank):
                with self.assertRaises(ValueError):
                    _validate_new_series_folder(self.layout, blank)

    def test_rejects_invalid_characters(self):
        for bad in ("Foo/Bar", "Foo\\Bar", "Re: Zero", 'Say "Hi"', "a<b", "a>b", "a|b", "a?b", "a*b"):
            with self.subTest(bad=bad):
                with self.assertRaises(ValueError):
                    _validate_new_series_folder(self.layout, bad)

    def test_rejects_trailing_dot_or_space(self):
        for bad in ("Foo.", "Foo. "):
            with self.subTest(bad=bad):
                with self.assertRaises(ValueError):
                    _validate_new_series_folder(self.layout, bad)

    def test_rejects_an_existing_franchise_case_insensitively(self):
        with self.assertRaises(ValueError):
            _validate_new_series_folder(self.layout, "genshin impact")

    def test_rejects_a_reserved_special_folder_first_segment(self):
        # Others/Artist's Original -> "Others"; Wallpaper -> "Wallpaper".
        for reserved in ("Others", "others", "Crossover", "Wallpaper"):
            with self.subTest(reserved=reserved):
                with self.assertRaises(ValueError):
                    _validate_new_series_folder(self.layout, reserved)

    def test_rejects_an_intercepting_alias_including_null(self):
        self.layout["franchise_aliases"] = {"Foo": "Azur Lane", "Bar": None}
        with self.assertRaises(ValueError) as cm:
            _validate_new_series_folder(self.layout, "Foo")
        self.assertIn("Azur Lane", str(cm.exception))
        with self.assertRaises(ValueError) as cm2:
            _validate_new_series_folder(self.layout, "Bar")
        self.assertIn("null", str(cm2.exception))

    def test_allows_an_alias_that_points_at_this_same_folder(self):
        # An alias whose value IS the folder does not make it unreachable.
        self.layout["franchise_aliases"] = {"NIKKE.": "NIKKE"}
        self.assertEqual(_validate_new_series_folder(self.layout, "NIKKE"), "NIKKE")


class CreateSeriesTest(SeriesFixture):
    def test_creates_a_flat_folder_appended_at_the_end(self):
        create_series("NIKKE", self.layout, self.path)
        self.assertEqual(self.layout["franchises"]["NIKKE"], {"style": "flat"})
        # Appended last -- hand-curated order preserved.
        self.assertEqual(list(self._on_disk()["franchises"]), [GENSHIN, AZUR, "NIKKE"])
        self.assertShortnameUntouched()

    def test_returns_the_layout_and_persists(self):
        returned = create_series("NIKKE", self.layout, self.path)
        self.assertIs(returned, self.layout)
        self.assertIn("NIKKE", self._on_disk()["franchises"])

    def test_future_images_route_into_the_new_folder(self):
        create_series("NIKKE", self.layout, self.path)
        result = self._route(_entry("NIKKE"))
        self.assertEqual(result.action, "move")
        self.assertEqual(result.dest_dir, "NIKKE")

    def test_refuses_and_writes_nothing_on_a_bad_name(self):
        raw, mtime = self._stamp()
        with self.assertRaises(ValueError):
            create_series("Genshin Impact", self.layout, self.path)
        self.assertEqual(self._raw(), raw)
        self.assertEqual(self.path.stat().st_mtime_ns, mtime)

    def test_write_is_lf_only(self):
        create_series("NIKKE", self.layout, self.path)
        self.assertNotIn(b"\r\n", self._raw())


class ApplySeriesPromotionTest(SeriesFixture):
    def test_creates_the_folder_and_routes_the_tag_instead_of_known_series(self):
        # Before: the tag falls through to Others/Known Series with a suffix.
        before = self._route(_entry("NIKKE"))
        self.assertEqual(before.dest_dir, "Others/Known Series")
        self.assertEqual(before.filename_suffix, "NK")

        report = _apply_series_promotion(
            self.layout, "NK", "NIKKE The Goddess of Victory", "NIKKE", ["NIKKE"], None
        )
        self.assertTrue(report["created"])
        self.assertEqual(report["folder"], "NIKKE")
        self.assertTrue(report["changed"])

        after = self._route(_entry("NIKKE"))
        self.assertEqual(after.action, "move")
        self.assertEqual(after.dest_dir, "NIKKE")

    def test_alias_key_is_the_canonicalized_tag(self):
        # A series alias maps the tag spelling to its canonical name; the
        # franchise-alias key written must be the canonical one, or
        # resolve_franchise (which canonicalizes first) would never read it.
        series_aliases = {"Nikke": "NIKKE The Goddess of Victory"}
        report = _apply_series_promotion(
            self.layout, "NK", "NIKKE The Goddess of Victory", "NIKKE",
            ["Nikke"], series_aliases,
        )
        keys = list(self.layout["franchise_aliases"])
        self.assertIn("NIKKE The Goddess of Victory", keys)
        self.assertEqual(report["folder"], "NIKKE")
        # And the raw tag still routes, through canonical -> franchise alias.
        self.assertEqual(self._route(_entry("Nikke")).dest_dir, "NIKKE")

    def test_existing_alias_is_reported_skipped_not_overwritten(self):
        # A tag with an explicit alias elsewhere; the new folder name is
        # distinct so it is not itself intercepted. The tag is left alone.
        self.layout["franchise_aliases"] = {"NIKKE": "Azur Lane"}
        report = _apply_series_promotion(
            self.layout, "NK", "NIKKE The Goddess of Victory", "NIKKE Global",
            ["NIKKE"], None,
        )
        self.assertEqual(self.layout["franchise_aliases"]["NIKKE"], "Azur Lane")
        self.assertIn(("NIKKE", "already aliased to Azur Lane"), report["skipped"])

    def test_uses_an_existing_flat_folder_without_creating(self):
        report = _apply_series_promotion(
            self.layout, "NK", "Azur Lane", AZUR, ["KanColle"], None
        )
        self.assertFalse(report["created"])
        self.assertEqual(report["folder"], AZUR)
        self.assertEqual(self.layout["franchise_aliases"]["KanColle"], AZUR)

    def test_refuses_a_non_flat_existing_folder(self):
        with self.assertRaises(ValueError):
            _apply_series_promotion(
                self.layout, "NK", "Genshin Impact", GENSHIN, ["Genshin"], None
            )

    def test_a_tag_that_already_lands_on_the_folder_does_nothing(self):
        # full_name equals the new folder, so it identity-resolves after
        # creation -- no self-alias, and it is not in aliases_added.
        report = _apply_series_promotion(
            self.layout, "NK", "NIKKE The Goddess of Victory",
            "NIKKE The Goddess of Victory", [], None,
        )
        self.assertEqual(report["aliases_added"], [])
        self.assertNotIn("franchise_aliases", {k: v for k, v in self.layout.items()
                                               if k == "franchise_aliases" and v})


class PreviewSeriesPromotionTest(SeriesFixture):
    def test_preview_never_mutates_the_caller_layout(self):
        snapshot = copy.deepcopy(self.layout)
        report = preview_series_promotion(
            self.layout, "NK", "NIKKE The Goddess of Victory", "NIKKE", ["NIKKE"], None
        )
        self.assertTrue(report["created"])
        self.assertEqual(self.layout, snapshot, "preview mutated the real layout")

    def test_preview_propagates_a_validation_error(self):
        with self.assertRaises(ValueError):
            preview_series_promotion(
                self.layout, "NK", "x", "Crossover", ["x"], None
            )


class PromoteSeriesTest(SeriesFixture):
    def test_saves_when_changed_and_returns_the_report(self):
        report = promote_series(
            "NK", "NIKKE The Goddess of Victory", "NIKKE", ["NIKKE"],
            self.layout, None, self.path,
        )
        self.assertTrue(report["changed"])
        self.assertIn("NIKKE", self._on_disk()["franchises"])
        self.assertShortnameUntouched()

    def test_no_save_when_nothing_changed(self):
        # Use an existing flat folder whose own name is the only tag -- it
        # identity-resolves, so no alias is added and no folder created.
        raw, mtime = self._stamp()
        report = promote_series(
            "NK", AZUR, AZUR, [AZUR], self.layout, None, self.path,
        )
        self.assertFalse(report["changed"])
        self.assertEqual(self._raw(), raw)
        self.assertEqual(self.path.stat().st_mtime_ns, mtime,
                         "an unchanged promotion must not rewrite the file")

    def test_refusal_leaves_the_file_untouched(self):
        raw, mtime = self._stamp()
        with self.assertRaises(ValueError):
            promote_series(
                "NK", "x", "Genshin Impact", ["x"], self.layout, None, self.path,
            )
        self.assertEqual(self._raw(), raw)
        self.assertEqual(self.path.stat().st_mtime_ns, mtime)

    def test_write_is_lf_only(self):
        promote_series(
            "NK", "NIKKE The Goddess of Victory", "NIKKE", ["NIKKE"],
            self.layout, None, self.path,
        )
        self.assertNotIn(b"\r\n", self._raw())


if __name__ == "__main__":
    unittest.main(verbosity=2)
