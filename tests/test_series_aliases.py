"""Regression tests for shortname.remove_series_alias.

Runs against a copy of the COMMITTED `data/series_aliases.example.json`, never
the real `data/series_aliases.json` -- that one is gitignored personal config,
so a test bound to it fails on every fresh clone and drags private series names
into a public repo. The example file is the same shape (a {"aliases": {...}}
envelope) with invented names, including a non-ASCII one.

remove_series_alias is the counterpart to save_series_alias, and it exists
because an alias answered wrongly at a review prompt is otherwise unretractable
without hand-editing JSON. That is how "Yuru Yuri" -> "Yuru Camp" survived,
pointing one series at another's shortname and misfiling its art.

What these pin down, in the order the bugs would bite:

  1. It actually removes, and the removal STICKS to disk -- a function that
     mutated only the returned dict would pass a naive in-memory assertion and
     change nothing on disk.
  2. The removed alias stops RESOLVING. Deleting the key is the mechanism;
     canonicalize_series no longer honouring it is the point.
  3. It matches case-insensitively and ignores trailing punctuation, on the
     same normalizer canonicalize_series uses to FIND an alias. An alias that
     resolves but cannot be removed by the name that resolves it is a trap.
  4. A missing variant is a clean no-op that does not rewrite the file.
     Asserted on the file BYTES, since a rewrite could reorder or reformat
     while leaving the parsed dict equal.
  5. Every other alias survives, and the envelope stays {"aliases": {...}} --
     save_series_aliases is what rebuilds the file, so a regression there would
     flatten it to a bare mapping and break load_series_aliases for every
     caller.

KNOWN LIMITATION, not asserted here because it is not this function's doing:
save_series_aliases rewrites the file as {"aliases": ...} alone, so a hand-added
top-level "_comment" (which the example file carries, and which
subreddit_map.json supports) is dropped by ANY alias write, save or remove.

    python -m unittest discover -s tests
    python tests/test_series_aliases.py
"""
import json
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from shortname import (  # noqa: E402
    canonicalize_series,
    load_series_aliases,
    remove_series_alias,
    save_series_alias,
)

EXAMPLE_ALIASES = REPO / "data" / "series_aliases.example.json"


class RemoveSeriesAliasTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="oshiire-aliases-"))
        self.path = self.tmp / "series_aliases.json"
        shutil.copy(EXAMPLE_ALIASES, self.path)
        self.before = load_series_aliases(self.path)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _raw(self):
        return self.path.read_bytes()

    def _envelope(self):
        return json.loads(self.path.read_text(encoding="utf-8"))

    # -- the fixture, so the assertions below mean something ----------------

    def test_fixture_has_the_properties_under_test(self):
        self.assertIn("aliases", self._envelope())
        self.assertGreaterEqual(len(self.before), 4)
        # Two variants pointing at one canonical, so removing one must not
        # disturb the other.
        self.assertEqual(self.before["Starfall"], "Starfall Chronicle")
        self.assertEqual(self.before["SFC"], "Starfall Chronicle")
        # A non-ASCII key, to prove the normalizer is not ASCII-only.
        self.assertIn("スターフォール", self.before)

    # -- 1. it removes, and the removal reaches disk ------------------------

    def test_removes_an_existing_alias(self):
        returned = remove_series_alias("SFC", self.path)

        self.assertNotIn("SFC", returned)
        self.assertNotIn("SFC", load_series_aliases(self.path),
                         "removal did not reach disk")
        self.assertEqual(len(returned), len(self.before) - 1)

    def test_removes_a_non_ascii_key(self):
        returned = remove_series_alias("スターフォール", self.path)

        self.assertNotIn("スターフォール", returned)
        self.assertNotIn("スターフォール", load_series_aliases(self.path))

    # -- 2. the removed alias stops resolving -------------------------------

    def test_removed_alias_stops_resolving(self):
        self.assertEqual(canonicalize_series("Starfall", self.before),
                         "Starfall Chronicle")

        after = remove_series_alias("Starfall", self.path)

        self.assertEqual(canonicalize_series("Starfall", after), "Starfall")

    def test_sibling_variant_keeps_resolving(self):
        """Two variants share one canonical; removing one must not orphan the
        other -- the bug that would quietly re-break the series it just fixed."""
        after = remove_series_alias("Starfall", self.path)

        self.assertEqual(canonicalize_series("SFC", after), "Starfall Chronicle")

    # -- 3. matches the way canonicalize_series matches ----------------------

    def test_matches_case_insensitively(self):
        """Anything canonicalize_series would honour must be retractable --
        including a casing the file does not literally store."""
        self.assertEqual(canonicalize_series("sFc", self.before), "Starfall Chronicle")

        after = remove_series_alias("sFc", self.path)

        self.assertNotIn("SFC", after)
        self.assertNotIn("SFC", load_series_aliases(self.path))

    def test_matches_trailing_punctuation_the_same_way(self):
        """_normalize_series_name strips trailing !/./whitespace, so a variant
        typed with one must still find its key."""
        after = remove_series_alias("Hollow Harbour.  ", self.path)

        self.assertNotIn("Hollow Harbour", after)

    # -- 4. missing variant is a clean no-op --------------------------------

    def test_no_ops_on_a_missing_variant(self):
        raw_before = self._raw()

        returned = remove_series_alias("No Such Series At All", self.path)

        self.assertEqual(returned, self.before)
        self.assertEqual(self._raw(), raw_before,
                         "a no-op must not rewrite the file at all")

    def test_no_ops_on_a_missing_file(self):
        missing = self.tmp / "nope.json"

        self.assertEqual(remove_series_alias("Anything", missing), {})
        self.assertFalse(missing.exists(), "a no-op must not create the file")

    # -- 5. everything else survives, envelope intact ------------------------

    def test_leaves_every_other_alias_untouched(self):
        expected = {k: v for k, v in self.before.items() if k != "SFC"}

        remove_series_alias("SFC", self.path)

        self.assertEqual(load_series_aliases(self.path), expected)

    def test_preserves_the_envelope(self):
        remove_series_alias("SFC", self.path)

        envelope = self._envelope()
        self.assertEqual(list(envelope), ["aliases"],
                         'the file must stay {"aliases": {...}}, not a bare mapping')
        self.assertIsInstance(envelope["aliases"], dict)

    def test_round_trips_with_save_series_alias(self):
        """The two are counterparts: save then remove returns to the start.
        Compared after a first normalizing write, since save_series_aliases
        drops the example file's _comment (see the module docstring)."""
        save_series_alias("Settling Write", "Starfall Chronicle", self.path)
        settled_raw = self._raw()
        settled = load_series_aliases(self.path)

        save_series_alias("Some Brand New Variant", "Lantern District", self.path)
        self.assertIn("Some Brand New Variant", load_series_aliases(self.path))

        remove_series_alias("Some Brand New Variant", self.path)

        self.assertEqual(load_series_aliases(self.path), settled)
        self.assertEqual(self._raw(), settled_raw,
                         "save_series_aliases sorts, so a round trip is byte-identical")

    def test_removes_every_casing_when_the_file_holds_two(self):
        """A hand-edited file can hold two casings of one name; leaving the
        survivor behind would keep the alias resolving after a 'removal'."""
        aliases = load_series_aliases(self.path)
        aliases["hollow harbour"] = "Hollow Harbour Mysteries"
        aliases["Hollow Harbour"] = "Hollow Harbour Mysteries"
        self.path.write_text(json.dumps({"aliases": aliases}), encoding="utf-8")

        after = remove_series_alias("HOLLOW HARBOUR", self.path)

        self.assertNotIn("hollow harbour", after)
        self.assertNotIn("Hollow Harbour", after)
        self.assertEqual(canonicalize_series("Hollow Harbour", after), "Hollow Harbour")


if __name__ == "__main__":
    unittest.main(verbosity=2)
