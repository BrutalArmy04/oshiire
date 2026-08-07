"""Regression tests for the shortname-file writers in shortname.py.

The shortname file (`000___Known_Series_Names.txt` here, `known_series_names.txt`
generically) maps SHORTNAME = Full Series Name, and the code is what gets
suffixed onto every filename filed under Others/Known Series. It is
hand-maintained: comments, blank lines and grouping are the user's, and no
writer may flatten them.

This covers `remove_shortname_entry`, the counterpart to `save_shortname_entry`
-- a code proposed and confirmed for the wrong series was previously
unretractable without hand-editing the file -- plus the deterministic-newline
guarantee both writers now carry.

What these pin down, in the order the bugs would bite:

  1. Save-then-remove round trip, byte for byte. The file is line-based
     precisely so nothing else moves; a writer that round-tripped through
     load_shortname_map's (code, full_name) list would pass a parsed-content
     assertion and silently eat every comment in the file.
  2. Removing an absent name is a clean no-op -- asserted on BOTH the bytes and
     the MTIME, since a rewrite producing identical bytes still churns mtime
     (and, on a Drive-synced folder, an upload).
  3. It matches on _normalize_series_name, the same rule save_shortname_entry
     uses to decide it is UPDATING a row rather than appending one. Anything
     that would have been overwritten in place must be removable.
  4. On-disk bytes are LF, never CRLF. Both writers open the tmp file with
     newline="" so the "\\n" they emit is not translated to the platform's line
     ending -- otherwise the file's bytes are decided by which machine wrote
     it, and this path is the one config file .gitattributes cannot cover
     (layout["shortname_file"] normally points outside the repo).

Runs on a synthetic file in a temp dir with an explicit path on every call.
Neither the real shortname file nor ARCHIVE_DIR is read or written.

    python -m unittest discover -s tests
    python tests/test_shortname_file.py
"""
import shutil
import sys
import tempfile
import time
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from shortname import (  # noqa: E402
    load_shortname_map,
    match_shortname,
    remove_shortname_entry,
    save_shortname_entry,
    undo_shortname_write,
)

# Deliberately shaped like a real hand-maintained file: a header comment, a
# blank line, an inline comment between entries, and trailing whitespace on one
# line. All of it must survive every write. Synthetic series names only.
FIXTURE = """\
# Known series shortnames -- SHORTNAME = Full Series Name
# One per line. Order is mine; leave it alone.

SC = Starfall Chronicle
LD = Lantern District

# Added later:
HHM = Hollow Harbour Mysteries!
NW = Neon Ward
"""


class ShortnameFileTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="oshiire-shortname-"))
        self.path = self.tmp / "known_series_names.txt"
        self.path.write_text(FIXTURE, encoding="utf-8", newline="")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _raw(self) -> bytes:
        return self.path.read_bytes()

    def _entries(self) -> list:
        return load_shortname_map({"shortname_file": str(self.path)})

    def _mtime_ns(self) -> int:
        return self.path.stat().st_mtime_ns


class RemoveShortnameEntryTest(ShortnameFileTestCase):
    # -- the fixture, so the assertions below mean something ----------------

    def test_fixture_has_the_properties_under_test(self):
        self.assertEqual(
            self._entries(),
            [("SC", "Starfall Chronicle"), ("LD", "Lantern District"),
             ("HHM", "Hollow Harbour Mysteries!"), ("NW", "Neon Ward")])
        self.assertIn("# One per line. Order is mine; leave it alone.", FIXTURE)
        self.assertIn("\n\n", FIXTURE, "fixture must contain a blank line")

    # -- 1. round trip -------------------------------------------------------

    def test_save_then_remove_round_trip_is_byte_identical(self):
        """The counterpart guarantee, asserted on BYTES: comments, blank lines,
        entry order and the trailing newline all come back exactly."""
        before = self._raw()

        save_shortname_entry(self.path, "VI", "Verdant Isle")
        self.assertIn(("VI", "Verdant Isle"), self._entries())
        self.assertNotEqual(self._raw(), before)

        remove_shortname_entry(self.path, "Verdant Isle")

        self.assertEqual(self._raw(), before)

    def test_removes_the_entry_and_nothing_else(self):
        remove_shortname_entry(self.path, "Lantern District")

        self.assertEqual(
            self._entries(),
            [("SC", "Starfall Chronicle"), ("HHM", "Hollow Harbour Mysteries!"),
             ("NW", "Neon Ward")])

    def test_preserves_comments_and_blank_lines(self):
        """Line-based, not a round trip through load_shortname_map -- that list
        has no representation for a comment, so a reader/writer round trip
        would delete the user's whole file structure."""
        remove_shortname_entry(self.path, "Lantern District")

        text = self.path.read_text(encoding="utf-8")
        self.assertIn("# Known series shortnames", text)
        self.assertIn("# One per line. Order is mine; leave it alone.", text)
        self.assertIn("# Added later:", text)
        self.assertIn("\n\n", text, "the blank line separator was eaten")
        self.assertNotIn("Lantern District", text)

    def test_removed_entry_stops_matching(self):
        """Deleting the line is the mechanism; match_shortname no longer
        resolving the tag to a code is the point."""
        self.assertEqual(match_shortname("Neon Ward", self._entries()), "NW")

        remove_shortname_entry(self.path, "Neon Ward")

        self.assertIsNone(match_shortname("Neon Ward", self._entries()))
        self.assertIsNone(match_shortname("NW", self._entries()))

    def test_removes_every_matching_line(self):
        """A hand-edited file can hold two spellings of one series under two
        codes; leaving the survivor behind would keep the tag matching after a
        'removal' -- and to the wrong code."""
        save_shortname_entry(self.path, "NW", "Neon Ward")
        self.path.write_text(
            self.path.read_text(encoding="utf-8") + "NW2 = neon ward.\n",
            encoding="utf-8", newline="")
        self.assertEqual(len([e for e in self._entries() if "eon" in e[1].lower()]), 2)

        remove_shortname_entry(self.path, "Neon Ward")

        self.assertEqual([e for e in self._entries() if "eon" in e[1].lower()], [])

    # -- 2. absent name is a clean no-op ------------------------------------

    def test_no_ops_on_a_missing_name(self):
        raw_before = self._raw()
        mtime_before = self._mtime_ns()
        time.sleep(0.01)

        remove_shortname_entry(self.path, "No Such Series At All")

        self.assertEqual(self._raw(), raw_before,
                         "a no-op must not rewrite the file at all")
        self.assertEqual(self._mtime_ns(), mtime_before,
                         "a no-op must not touch the file's mtime")

    def test_no_ops_on_a_missing_file(self):
        missing = self.tmp / "nope.txt"

        remove_shortname_entry(missing, "Starfall Chronicle")

        self.assertFalse(missing.exists(), "a no-op must not create the file")

    def test_no_ops_on_an_empty_name(self):
        """`CODE = ` parses to a full_name of "", so an empty query must not be
        allowed to match and delete a malformed row."""
        raw_before = self._raw()

        remove_shortname_entry(self.path, "")
        remove_shortname_entry(self.path, "   ")

        self.assertEqual(self._raw(), raw_before)

    def test_a_comment_mentioning_the_name_is_not_a_match(self):
        """_parse_shortname_line returns None for a comment, so only real
        mapping lines are candidates."""
        self.path.write_text(
            "# Verdant Isle = VI, decide later\nSC = Starfall Chronicle\n",
            encoding="utf-8", newline="")

        remove_shortname_entry(self.path, "Verdant Isle")

        self.assertIn("# Verdant Isle = VI, decide later",
                      self.path.read_text(encoding="utf-8"))

    # -- 3. matches the way the writer matches -------------------------------

    def test_removes_by_a_casing_variant(self):
        """save_shortname_entry would have UPDATED this row in place rather
        than appending a second one, so remove must be able to reach it."""
        remove_shortname_entry(self.path, "sTaRfAlL cHrOnIcLe")

        self.assertNotIn(("SC", "Starfall Chronicle"), self._entries())

    def test_removes_by_a_trailing_punctuation_variant(self):
        """_normalize_series_name strips trailing !/./whitespace -- the rule
        that lets "Bocchi the rock!" reuse "Bocchi the Rock"'s code."""
        remove_shortname_entry(self.path, "Hollow Harbour Mysteries  ")

        self.assertNotIn("Hollow Harbour", self.path.read_text(encoding="utf-8"))

    def test_a_different_series_with_a_shared_prefix_is_not_removed(self):
        """match_shortname deliberately matches a leading token ("NIKKE" finds
        "NIKKE The Goddess of Victory"); removal must NOT -- it deletes, so a
        prefix match here would take out the wrong series."""
        save_shortname_entry(self.path, "NWA", "Neon Ward Aftermath")

        remove_shortname_entry(self.path, "Neon Ward")

        self.assertIn(("NWA", "Neon Ward Aftermath"), self._entries())
        self.assertNotIn(("NW", "Neon Ward"), self._entries())


class ShortnameFileNewlineTest(ShortnameFileTestCase):
    """Every write site must produce LF on disk regardless of platform.

    Text mode's default (newline=None) translates the "\\n" these writers emit
    into the OS line ending, so the same code produced CRLF on Windows and LF
    on Linux -- the file's bytes decided by the machine, not the code. This
    path matters more than the JSON ones: layout["shortname_file"] normally
    points OUTSIDE the repo, so .gitattributes cannot reach it and newline=""
    is the only mechanism available.
    """

    def test_save_writes_lf_only(self):
        save_shortname_entry(self.path, "VI", "Verdant Isle")

        raw = self._raw()
        self.assertNotIn(b"\r\n", raw)
        self.assertIn(b"VI = Verdant Isle\n", raw)

    def test_remove_writes_lf_only(self):
        remove_shortname_entry(self.path, "Lantern District")

        self.assertNotIn(b"\r\n", self._raw())

    def test_save_normalizes_a_crlf_file_to_lf(self):
        """The intended one-time reformat: a file already on disk as CRLF (this
        user's is) becomes LF on its next write, rather than staying mixed."""
        self.path.write_bytes(FIXTURE.replace("\n", "\r\n").encode("utf-8"))
        self.assertIn(b"\r\n", self._raw())

        save_shortname_entry(self.path, "VI", "Verdant Isle")

        self.assertNotIn(b"\r\n", self._raw())
        self.assertEqual(len(self._entries()), 5,
                         "splitlines() must still have parsed the CRLF input")

    def test_undo_does_not_reintroduce_crlf(self):
        """undo_shortname_write restores a snapshot taken before the save. Its
        write must not re-translate those "\\n"s, or an undo would put back the
        CRLF the writer just stopped producing."""
        snapshot = self.path.read_text(encoding="utf-8")
        save_shortname_entry(self.path, "VI", "Verdant Isle")

        undo_shortname_write(self.path, True, snapshot)

        self.assertNotIn(b"\r\n", self._raw())
        self.assertEqual(self._raw(), FIXTURE.encode("utf-8"))

    def test_undo_of_a_created_file_still_deletes_it(self):
        fresh = self.tmp / "fresh.txt"
        save_shortname_entry(fresh, "VI", "Verdant Isle")
        self.assertTrue(fresh.exists())

        undo_shortname_write(fresh, False, None)

        self.assertFalse(fresh.exists())

    def test_no_tmp_file_is_left_behind(self):
        """Both writers go tmp + os.replace; a leftover .tmp next to a
        user-visible config file is confusing at best."""
        save_shortname_entry(self.path, "VI", "Verdant Isle")
        remove_shortname_entry(self.path, "Verdant Isle")

        self.assertEqual(sorted(p.name for p in self.tmp.iterdir()),
                         ["known_series_names.txt"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
