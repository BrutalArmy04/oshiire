"""Regression tests for subreddit_map.json editing (review.py's Settings tab).

Runs against a COPY of the real subreddit_map.json, because the properties
worth protecting are properties of that file: a top-level `_comment`, eight
entries carrying a hand-written `_note`, and seven whose franchise is JSON
null. The real file is never written.

What these pin down, in the order the bugs would bite:

  1. read_subreddit_map RAISES on a missing file. Its sibling
     _load_subreddit_map calls sys.exit(1), which is right for a CLI and fatal
     in a Gradio handler -- an exit there takes down the server for every tab.
  2. save_subreddit_map_entry MERGES. It used to rebuild the entry from
     {"franchise": ...} + optional "character", so saving any of the eight
     noted entries deleted its note silently.
  3. A null franchise survives an unrelated save. null means "parse the
     franchise from the title" -- it is a shape, not a missing value.
  4. `_comment` survives every write.
  5. Key order is otherwise untouched, and a rename lands the new key at the
     end (accepted, but the UI has to say so).

    python -m unittest discover -s tests
    python tests/test_subreddit_map_editor.py
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

import tagger  # noqa: E402

REAL_MAP = REPO / "subreddit_map.json"


def _load(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


class SubredditMapEditorTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="oshiire-submap-"))
        self.path = self.tmp / "subreddit_map.json"
        shutil.copy(REAL_MAP, self.path)
        self.before = _load(self.path)
        self.entries_before = {k: v for k, v in self.before.items() if not k.startswith("_")}

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    # -- the shape of the real file, so the numbers below mean something ----

    def test_fixture_has_the_properties_under_test(self):
        self.assertIn("_comment", self.before)
        notes = [k for k, v in self.entries_before.items() if "_note" in v]
        nulls = [k for k, v in self.entries_before.items()
                 if "franchise" in v and v["franchise"] is None]
        self.assertEqual(len(notes), 8, f"expected 8 noted entries, got {notes}")
        self.assertEqual(len(nulls), 7, f"expected 7 null franchises, got {nulls}")
        self.assertEqual(len(self.entries_before), 167)

    # -- hazard 1: the public reader raises, it does not exit ---------------

    def test_read_subreddit_map_raises_on_missing_file(self):
        missing = self.tmp / "nope.json"
        with self.assertRaises(FileNotFoundError):
            tagger.read_subreddit_map(missing)

    def test_private_loader_still_exits_on_missing_file(self):
        """The CLI paths depend on the exit; only the new reader differs."""
        missing = self.tmp / "nope.json"
        with self.assertRaises(SystemExit) as ctx:
            tagger._load_subreddit_map(missing)
        self.assertEqual(ctx.exception.code, 1)

    def test_read_subreddit_map_keeps_underscore_keys_and_casing(self):
        raw = tagger.read_subreddit_map(self.path)
        self.assertIn("_comment", raw, "`_comment` must survive the read to survive the write")
        self.assertEqual(list(raw), list(self.before), "key order/content must be verbatim")
        # and the consumer-facing loader still strips + lowercases
        loaded = tagger._load_subreddit_map(self.path)
        self.assertNotIn("_comment", loaded)
        self.assertTrue(all(k == k.lower() for k in loaded))

    # -- hazards 2/3/4: an unrelated save must disturb nothing else ---------

    def test_unrelated_save_changes_exactly_one_entry(self):
        # A sentinel franchise, not a real correction: the entry's current
        # value must not decide whether this test exercises anything. (It did
        # once -- the test wrote asuka's real fix, and stopped detecting a
        # change the moment that fix landed in the file.)
        target = "asuka"
        self.assertIn(target, self.entries_before)
        tagger.save_subreddit_map_entry(target, "ZZ Sentinel Franchise",
                                        "ZZ Sentinel Character", path=self.path)
        after = _load(self.path)
        self.assertNotEqual(after[target], self.before[target],
                            "sentinel save must actually change the entry")

        changed = [k for k in set(self.before) | set(after)
                   if self.before.get(k, object()) != after.get(k, object())]
        self.assertEqual(changed, [target], f"expected only {target!r} to change, got {changed}")

        self.assertIn("_comment", after)
        self.assertEqual(after["_comment"], self.before["_comment"])

        notes_before = {k: v["_note"] for k, v in self.entries_before.items() if "_note" in v}
        notes_after = {k: v["_note"] for k, v in after.items()
                       if not k.startswith("_") and "_note" in v}
        self.assertEqual(notes_after, notes_before, "all 8 `_note` keys must survive")

        nulls_after = [k for k, v in after.items()
                       if not k.startswith("_") and "franchise" in v and v["franchise"] is None]
        nulls_before = [k for k, v in self.entries_before.items()
                        if "franchise" in v and v["franchise"] is None]
        self.assertEqual(nulls_after, nulls_before, "the 7 null franchises must stay null")

        self.assertEqual(list(after), list(self.before), "key order must be unchanged")

    def test_saving_a_noted_entry_preserves_its_note(self):
        """The specific regression: 8 entries carry a note, and one of those
        carries a character too."""
        for key in [k for k, v in self.entries_before.items() if "_note" in v]:
            with self.subTest(entry=key):
                note = self.before[key]["_note"]
                tagger.save_subreddit_map_entry(key, "Rewritten Franchise", path=self.path)
                after = _load(self.path)
                self.assertEqual(after[key].get("_note"), note, f"{key}: note was destroyed")
                self.assertEqual(after[key]["franchise"], "Rewritten Franchise")

    def test_null_franchise_round_trips(self):
        tagger.save_subreddit_map_entry("awwnime", None, path=self.path)
        after = _load(self.path)
        self.assertIn("franchise", after["awwnime"])
        self.assertIsNone(after["awwnime"]["franchise"])
        # written as JSON null, not the string "None" or ""
        self.assertIn('"franchise": null', self.path.read_text(encoding="utf-8"))

    def test_empty_character_clears_the_key(self):
        self.assertIn("character", self.before["acheronmains"])
        note = self.before["acheronmains"]["_note"]
        tagger.save_subreddit_map_entry("acheronmains", "Honkai: Star Rail", None, path=self.path)
        after = _load(self.path)
        self.assertNotIn("character", after["acheronmains"])
        self.assertEqual(after["acheronmains"]["_note"], note, "clearing a character kept the note")

    # -- counts and rename ordering ----------------------------------------

    def test_entry_count_across_save_delete_readd(self):
        def count():
            return len([k for k in _load(self.path) if not k.startswith("_")])

        self.assertEqual(count(), 167)
        tagger.save_subreddit_map_entry("asuka", "Evangelion", "Asuka", path=self.path)
        self.assertEqual(count(), 167, "saving an existing entry must not change the count")
        tagger.remove_subreddit_map_entry("asuka", path=self.path)
        self.assertEqual(count(), 166)
        tagger.save_subreddit_map_entry("asuka", "Evangelion", "Asuka", path=self.path)
        self.assertEqual(count(), 167)

    def test_rename_puts_the_new_key_at_the_end(self):
        """Accepted behaviour -- the Settings tab's status line says so."""
        tagger.remove_subreddit_map_entry("asuka", path=self.path)
        tagger.save_subreddit_map_entry("asukalangley", "Evangelion", "Asuka", path=self.path)
        after = _load(self.path)
        self.assertNotIn("asuka", after)
        self.assertEqual(list(after)[-1], "asukalangley")
        self.assertEqual(len([k for k in after if not k.startswith("_")]), 167)

    def test_new_entry_is_appended_and_disturbs_nothing(self):
        tagger.save_subreddit_map_entry("brandnewsub", "Some Series", path=self.path)
        after = _load(self.path)
        self.assertEqual(list(after)[-1], "brandnewsub")
        self.assertEqual(list(after)[:-1], list(self.before), "existing key order must be intact")


if __name__ == "__main__":
    unittest.main(verbosity=2)
