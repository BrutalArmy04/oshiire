"""Regression tests for shortname.remove_character_alias.

The counterpart to save_character_alias, and the character-level twin of
remove_series_alias. It exists for the same reason that one does: the review
UI's "remember this name?" prompt writes an alias on one confirm, and answering
it with the wrong target silently re-points every future tag of that name into
another character's folder. Until this function there was no way back but
hand-editing layout.json.

What these pin down, in the order the bugs would bite:

  1. It actually removes, and the removal reaches DISK. A function that mutated
     only the passed-in dict would satisfy a naive in-memory assertion and
     change nothing on the file the next launch reads.
  2. The removed alias stops RESOLVING. Deleting the key is the mechanism;
     resolve_character no longer honouring it is the point.
  3. It matches on the CHARACTER normalizer (normalize_name_key), not
     lookup_ci's series rule. That is the one place this differs from
     remove_series_alias, and it is load-bearing: a variant read off a UI label
     is spelled however the post spelled it, so an alias that resolves but
     cannot be retracted by the name that resolves it is a trap.
  4. An absent franchise or key is a clean no-op that does not rewrite the
     file. Asserted on the file BYTES, since a rewrite could reorder
     layout.json's hand-curated keys while leaving the parsed dict equal.
  5. Emptied containers are pruned, so an add-then-remove round trip leaves
     layout.json exactly as it started -- and everything else in the layout,
     including other franchises' aliases, survives untouched.

Runs entirely on a synthetic layout in a temp dir, with an explicit path= on
every write. Neither the real layout.json nor ARCHIVE_DIR is read or written.

    python -m unittest discover -s tests
    python tests/test_character_aliases.py
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

from shortname import (  # noqa: E402
    load_layout,
    remove_character_alias,
    resolve_character,
    save_character_alias,
)

FOLDER = "Starfall Chronicle"
OTHER = "Lantern District"

BASE_LAYOUT = {
    "group_subfolder": "Others_Group",
    "franchise_aliases": {},
    "character_aliases": {},
    "franchises": {
        FOLDER: {"style": "nested", "characters": ["Sera", "Kestrel", "Vela Quinn"]},
        OTHER: {"style": "nested", "characters": ["Yuzu Hoshimi"]},
    },
}


class RemoveCharacterAliasTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="oshiire-charalias-"))
        self.path = self.tmp / "layout.json"
        self.layout = copy.deepcopy(BASE_LAYOUT)
        self.path.write_text(json.dumps(self.layout, indent=2), encoding="utf-8")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _on_disk(self) -> dict:
        return load_layout(self.path)

    def _raw(self) -> bytes:
        return self.path.read_bytes()

    def _aliases(self, layout=None, folder=FOLDER) -> dict:
        return (layout or self.layout).get("character_aliases", {}).get(folder, {})

    def _resolve(self, name, layout=None, folder=FOLDER):
        layout = layout or self.layout
        return resolve_character(folder, layout["franchises"][folder], name, layout)

    # -- 1. it removes, and the removal reaches disk ------------------------

    def test_add_then_remove_round_trip(self):
        """The whole point: an alias recorded by one confirm is retractable by
        one click, and layout.json goes back to exactly what it was.

        Started from a layout with no `character_aliases` key at all, because
        that is the only starting state a round trip can restore exactly --
        pruning is by emptiness, so a layout that shipped an EMPTY table gets
        it pruned too (see test_an_emptied_table_is_pruned_even_if_it_shipped_
        empty, where that difference is asserted rather than worked around)."""
        del self.layout["character_aliases"]
        self.path.write_text(json.dumps(self.layout, indent=2), encoding="utf-8")
        before = self._raw()

        save_character_alias(FOLDER, "Serah", "Sera", self.layout, self.path)
        self.assertEqual(self._aliases(), {"Serah": "Sera"})
        self.assertEqual(self._on_disk()["character_aliases"][FOLDER], {"Serah": "Sera"})
        self.assertNotEqual(self._raw(), before)

        returned = remove_character_alias(FOLDER, "Serah", self.layout, self.path)

        self.assertEqual(self._aliases(returned), {})
        self.assertEqual(self._aliases(self._on_disk()), {},
                         "removal did not reach disk")
        self.assertEqual(json.loads(self._raw()), json.loads(before))

    def test_returns_the_updated_layout(self):
        save_character_alias(FOLDER, "Serah", "Sera", self.layout, self.path)

        returned = remove_character_alias(FOLDER, "Serah", self.layout, self.path)

        self.assertIs(returned, self.layout)

    # -- 2. the removed alias stops resolving -------------------------------

    def test_removed_alias_stops_resolving(self):
        save_character_alias(FOLDER, "Kes", "Kestrel", self.layout, self.path)
        self.assertEqual(self._resolve("Kes"), "Kestrel")

        remove_character_alias(FOLDER, "Kes", self.layout, self.path)

        self.assertIsNone(self._resolve("Kes"))

    def test_the_alias_target_itself_still_resolves(self):
        """Retracting variant -> canonical must not touch the roster entry the
        alias pointed AT -- that is a real folder on disk."""
        save_character_alias(FOLDER, "Kes", "Kestrel", self.layout, self.path)

        remove_character_alias(FOLDER, "Kes", self.layout, self.path)

        self.assertEqual(self._resolve("Kestrel"), "Kestrel")

    # -- 3. matches the way CHARACTER matching matches -----------------------

    def test_removes_by_a_casing_variant(self):
        """Anything resolve_character would honour must be retractable --
        including a casing layout.json does not literally store."""
        save_character_alias(FOLDER, "Serah", "Sera", self.layout, self.path)
        self.assertEqual(self._resolve("SERAH"), "Sera")

        remove_character_alias(FOLDER, "SERAH", self.layout, self.path)

        self.assertEqual(self._aliases(), {})
        self.assertEqual(self._aliases(self._on_disk()), {})

    def test_removes_by_a_spacing_or_punctuation_variant(self):
        """The load-bearing difference from remove_series_alias: character
        matching uses normalize_name_key, which drops every non-alphanumeric.
        lookup_ci's series normalizer only casefolds, so it would miss all of
        these -- and a variant read off a UI label is spelled however the post
        that raised the prompt spelled it."""
        for variant in ("veequinn", "VEE-QUINN", "vee  quinn", "Vee.Quinn"):
            with self.subTest(variant=variant):
                save_character_alias(FOLDER, "Vee Quinn", "Vela Quinn", self.layout, self.path)
                self.assertEqual(self._resolve("Vee Quinn"), "Vela Quinn")

                remove_character_alias(FOLDER, variant, self.layout, self.path)

                self.assertEqual(self._aliases(), {})
                self.assertEqual(self._aliases(self._on_disk()), {})
                self.assertIsNone(self._resolve("Vee Quinn"))

    def test_removes_every_key_that_normalizes_to_the_target(self):
        """A hand-edited layout.json can hold two spellings of one variant;
        leaving the survivor behind would keep the alias resolving after a
        'removal'."""
        self.layout["character_aliases"][FOLDER] = {
            "serah": "Sera", "Serah": "Sera", "SE-RAH": "Sera", "Kes": "Kestrel",
        }

        remove_character_alias(FOLDER, "Serah", self.layout, self.path)

        self.assertEqual(self._aliases(), {"Kes": "Kestrel"})
        self.assertIsNone(self._resolve("serah"))
        self.assertEqual(self._resolve("Kes"), "Kestrel")

    # -- 4. absent target is a clean no-op ----------------------------------

    def test_no_ops_on_a_missing_variant(self):
        save_character_alias(FOLDER, "Serah", "Sera", self.layout, self.path)
        raw_before = self._raw()

        remove_character_alias(FOLDER, "Nobody At All", self.layout, self.path)

        self.assertEqual(self._raw(), raw_before,
                         "a no-op must not rewrite the file at all")
        self.assertEqual(self._aliases(), {"Serah": "Sera"})

    def test_no_ops_on_a_missing_franchise(self):
        save_character_alias(FOLDER, "Serah", "Sera", self.layout, self.path)
        raw_before = self._raw()

        remove_character_alias("No Such Franchise", "Serah", self.layout, self.path)

        self.assertEqual(self._raw(), raw_before)
        self.assertEqual(self._aliases(), {"Serah": "Sera"})

    def test_no_ops_on_a_layout_with_no_character_aliases_key(self):
        """The table is not guaranteed present -- a hand-written layout.json
        may never have had one, and the prune below can delete it."""
        del self.layout["character_aliases"]
        self.path.write_text(json.dumps(self.layout, indent=2), encoding="utf-8")
        raw_before = self._raw()

        returned = remove_character_alias(FOLDER, "Serah", self.layout, self.path)

        self.assertNotIn("character_aliases", returned)
        self.assertEqual(self._raw(), raw_before)

    def test_no_ops_on_an_empty_variant(self):
        save_character_alias(FOLDER, "Serah", "Sera", self.layout, self.path)
        raw_before = self._raw()

        remove_character_alias(FOLDER, "", self.layout, self.path)
        remove_character_alias(FOLDER, "   ", self.layout, self.path)

        self.assertEqual(self._raw(), raw_before)
        self.assertEqual(self._aliases(), {"Serah": "Sera"})

    # -- 5. pruning, and everything else survives ---------------------------

    def test_prunes_the_emptied_franchise_and_table(self):
        """Retracting the only alias must leave no empty husks behind -- the
        round-trip guarantee in test_add_then_remove_round_trip depends on it,
        and this asserts the mechanism directly."""
        del self.layout["character_aliases"]
        save_character_alias(FOLDER, "Serah", "Sera", self.layout, self.path)
        self.assertIn("character_aliases", self.layout)

        remove_character_alias(FOLDER, "Serah", self.layout, self.path)

        self.assertNotIn("character_aliases", self.layout)
        self.assertNotIn("character_aliases", self._on_disk())

    def test_a_no_op_never_prunes(self):
        """BASE_LAYOUT ships `"character_aliases": {}`. Pruning is reached only
        after a real removal, so a no-op leaves even an empty table alone --
        which is what makes every no-op assertion above a byte-level one."""
        remove_character_alias(FOLDER, "Serah", self.layout, self.path)

        self.assertEqual(self.layout["character_aliases"], {})
        self.assertEqual(self._on_disk()["character_aliases"], {})

    def test_an_emptied_table_is_pruned_even_if_it_shipped_empty(self):
        """Pruning keys on emptiness, not on who created the table, so a
        layout.json that shipped `"character_aliases": {}` loses that key once
        an alias is added and then retracted. Semantically identical -- every
        reader goes through .get() -- and it is what keeps the round trip above
        clean rather than leaving a husk per franchise ever aliased."""
        save_character_alias(FOLDER, "Serah", "Sera", self.layout, self.path)

        remove_character_alias(FOLDER, "Serah", self.layout, self.path)

        self.assertNotIn("character_aliases", self.layout)
        self.assertEqual(self._aliases(self._on_disk()), {})

    def test_keeps_the_other_aliases_and_the_other_franchises(self):
        """character_aliases is scoped per franchise because two franchises can
        have different characters sharing one short name."""
        save_character_alias(FOLDER, "Serah", "Sera", self.layout, self.path)
        save_character_alias(FOLDER, "Kes", "Kestrel", self.layout, self.path)
        save_character_alias(OTHER, "Serah", "Yuzu Hoshimi", self.layout, self.path)

        remove_character_alias(FOLDER, "Serah", self.layout, self.path)

        reloaded = self._on_disk()
        self.assertEqual(self._aliases(reloaded), {"Kes": "Kestrel"})
        self.assertEqual(self._aliases(reloaded, OTHER), {"Serah": "Yuzu Hoshimi"})
        self.assertEqual(self._resolve("Serah", reloaded, OTHER), "Yuzu Hoshimi")
        self.assertIsNone(self._resolve("Serah", reloaded))

    def test_leaves_the_rest_of_the_layout_alone(self):
        """It writes through save_layout, which re-dumps the whole file -- a
        regression there would take the roster and the franchise map with it."""
        save_character_alias(FOLDER, "Serah", "Sera", self.layout, self.path)

        remove_character_alias(FOLDER, "Serah", self.layout, self.path)

        reloaded = self._on_disk()
        self.assertEqual(reloaded["franchises"], BASE_LAYOUT["franchises"])
        self.assertEqual(reloaded["group_subfolder"], "Others_Group")
        self.assertEqual(reloaded["franchise_aliases"], {})

    # -- on-disk bytes ------------------------------------------------------

    def test_write_is_lf_only(self):
        """save_layout opens the tmp file with newline="" so its "\\n"s reach
        disk untranslated. Without that, these bytes are CRLF on Windows and LF
        on Linux -- the file's content decided by which machine wrote it."""
        save_character_alias(FOLDER, "Serah", "Sera", self.layout, self.path)
        self.assertNotIn(b"\r\n", self._raw())

        remove_character_alias(FOLDER, "Serah", self.layout, self.path)
        self.assertNotIn(b"\r\n", self._raw())
        self.assertIn(b"\n", self._raw(), "indent=2 must still produce newlines")


if __name__ == "__main__":
    unittest.main(verbosity=2)
