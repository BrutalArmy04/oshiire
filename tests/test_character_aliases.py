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

import archive  # noqa: E402
from shortname import (  # noqa: E402
    CHARACTER_ALIAS_DISMISSED_KEY,
    CHARACTER_GROUP_ROUTE_KEY,
    load_layout,
    merge_character,
    promote_character,
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


# ===========================================================================
# promote_character / merge_character
#
# The intent-level pair on top of the writers above: "give this name its own
# folder" and "group this name under another folder."
#
# What these pin down, in the order the bugs would bite:
#
#   1. Promoting takes BOTH edits. resolve_character reads character_aliases
#      before the roster, so a name aliased to a group folder keeps resolving
#      there no matter what the roster says -- adding the roster entry alone is
#      a silent no-op, and that is the bug the whole pair exists to prevent.
#      Asserted through resolve_character AND through archive.route_entry, so
#      the claim is about where the file lands, not about dict contents.
#   2. A stale group-route pin is cleared too. archive.py honours a pin ahead
#      of a roster match, so a promotion that left one behind would keep filing
#      the character into the group folder while the new folder sat empty.
#   3. Nothing else moves. The group folder stays on the roster and its other
#      aliased names keep resolving to it -- promoting one Fatui must not
#      unhook the rest.
#   4. Already-satisfied calls do not rewrite the file AT ALL. Asserted on the
#      bytes AND on st_mtime_ns: layout.json is hand-curated and Drive-synced,
#      so a no-op rewrite is not free even when it round-trips equal.
#   5. Both edits reach disk together or neither does. A ValueError (unknown
#      franchise, or an `into` that is not a real folder) must leave the file
#      untouched -- an alias pointing at a folder that doesn't exist looks,
#      from the review UI, exactly like an alias that was never recorded.
#   6. promote then merge back restores ROUTING, asserted via route_entry
#      rather than byte-identity: roster order legitimately differs after a
#      round trip, and pinning bytes here would fail on a correct result.
#
# The fixture mirrors the real Genshin Impact shape -- a nested franchise whose
# roster carries the "Fatui" group folder, with the individual Fatui aliased
# into it -- because that is the layout the pair was written for.
# ===========================================================================

GENSHIN = "Genshin Impact"
HSR = "Honkai: Star Rail"

PROMOTE_LAYOUT = {
    "group_subfolder": "Others_Group",
    "special_folders": {
        "crossover": "Crossover",
        "others_oc": "Others/Artist's Original",
        "others_unknown_source": "Others/Unknown Sauce",
        "others_known_series": "Others/Known Series",
    },
    "shortname_file": "known_series_names.txt",
    "franchise_aliases": {},
    "character_aliases": {
        GENSHIN: {"Signora": "Fatui", "Sandrone": "Fatui", "Arlecchino": "Fatui"},
        HSR: {"Kafka": "Stellaron Hunters"},
    },
    "franchises": {
        GENSHIN: {
            "style": "nested",
            "characters": ["Hu Tao", "Raiden Shogun", "Kuki Shinobu", "Fatui"],
        },
        HSR: {"style": "nested", "characters": ["Stellaron Hunters", "Firefly"]},
    },
}


def _entry(characters, franchise=GENSHIN, **extra):
    entry = {
        "post_id": "t3_test",
        "title": "Test",
        "franchise": [franchise],
        "character_guess": list(characters),
        "crossover": False,
    }
    entry.update(extra)
    return entry


class PromoteMergeTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="oshiire-promote-"))
        self.path = self.tmp / "layout.json"
        self.layout = copy.deepcopy(PROMOTE_LAYOUT)
        self.path.write_text(json.dumps(self.layout, indent=2), encoding="utf-8")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _on_disk(self) -> dict:
        return load_layout(self.path)

    def _raw(self) -> bytes:
        return self.path.read_bytes()

    def _stamp(self):
        """(bytes, st_mtime_ns) -- the pair every no-op assertion compares.
        Bytes alone would pass a rewrite that happened to reproduce the same
        content; mtime alone would pass a rewrite that changed it."""
        return self._raw(), self.path.stat().st_mtime_ns

    def _aliases(self, layout=None, folder=GENSHIN) -> dict:
        return (layout or self.layout).get("character_aliases", {}).get(folder, {})

    def _roster(self, layout=None, folder=GENSHIN) -> list:
        return (layout or self.layout)["franchises"][folder]["characters"]

    def _resolve(self, name, layout=None, folder=GENSHIN):
        layout = layout or self.layout
        return resolve_character(folder, layout["franchises"][folder], name, layout)

    def _route(self, entry, layout=None):
        return archive.route_entry(entry, layout or self.layout, [], None)


class PromoteCharacterTest(PromoteMergeTestCase):
    # -- 1. both edits, and the promotion actually resolves ------------------

    def test_promote_drops_the_alias_and_adds_the_roster_entry(self):
        """The headline case. Dropping the alias is what makes the roster entry
        reachable at all -- resolve_character checks character_aliases first."""
        self.assertEqual(self._resolve("Sandrone"), "Fatui")

        promote_character(GENSHIN, "Sandrone", self.layout, self.path)

        self.assertNotIn("Sandrone", self._aliases())
        self.assertIn("Sandrone", self._roster())
        self.assertEqual(self._resolve("Sandrone"), "Sandrone")

        reloaded = self._on_disk()
        self.assertNotIn("Sandrone", self._aliases(reloaded))
        self.assertIn("Sandrone", self._roster(reloaded))
        self.assertEqual(self._resolve("Sandrone", reloaded), "Sandrone",
                         "the promotion did not reach disk")

    def test_returns_the_updated_layout(self):
        returned = promote_character(GENSHIN, "Sandrone", self.layout, self.path)
        self.assertIs(returned, self.layout)

    def test_promote_resolves_the_franchise_folder_case_insensitively(self):
        """Every write goes through the CONFIGURED key: character_aliases and
        character_group_route have no case-insensitive read path of their own,
        so writing under a caller's casing builds a second table nothing
        reads."""
        promote_character("genshin impact", "Sandrone", self.layout, self.path)

        self.assertEqual(list(self._on_disk()["franchises"]), [GENSHIN, HSR])
        self.assertEqual(list(self._on_disk()["character_aliases"]), [GENSHIN, HSR])
        self.assertEqual(self._resolve("Sandrone"), "Sandrone")

    def test_promote_drops_the_alias_by_a_spelling_variant(self):
        """The name arrives from a UI label spelled however the post spelled
        it, and _alias_drop matches on normalize_name_key -- the same rule that
        made the alias resolve in the first place."""
        promote_character(GENSHIN, "sand-rone", self.layout, self.path)

        self.assertNotIn("Sandrone", self._aliases())
        self.assertEqual(self._resolve("Sandrone"), "sand-rone")

    # -- 2. routing, end to end ---------------------------------------------

    def test_route_entry_moves_from_the_group_folder_to_the_new_one(self):
        """The whole point stated as a destination path, which is what the user
        actually sees."""
        entry = _entry(["Sandrone"])
        self.assertEqual(self._route(entry).dest_dir, f"{GENSHIN}/Fatui")

        promote_character(GENSHIN, "Sandrone", self.layout, self.path)

        result = self._route(entry)
        self.assertEqual(result.action, "move")
        self.assertEqual(result.dest_dir, f"{GENSHIN}/Sandrone")

    def test_promote_clears_a_group_route_pin(self):
        """archive.py honours a pin AHEAD of a roster match, so a promotion
        that left one behind would file the character into the group folder
        while its new subfolder sat empty."""
        self.layout[CHARACTER_GROUP_ROUTE_KEY] = {GENSHIN: ["Sandrone", "Childe"]}
        self.path.write_text(json.dumps(self.layout, indent=2), encoding="utf-8")

        promote_character(GENSHIN, "Sandrone", self.layout, self.path)

        self.assertEqual(self._on_disk()[CHARACTER_GROUP_ROUTE_KEY][GENSHIN], ["Childe"])
        self.assertEqual(self._route(_entry(["Sandrone"])).dest_dir, f"{GENSHIN}/Sandrone")
        self.assertEqual(self._route(_entry(["Childe"])).dest_dir, f"{GENSHIN}/Others_Group")

    def test_promote_leaves_the_dismissal_table_alone(self):
        """A dismissal is review-side only and has no routing effect, so
        promoting must not silently re-open the prompt for the name."""
        self.layout[CHARACTER_ALIAS_DISMISSED_KEY] = {GENSHIN: ["Sandrone"]}
        self.path.write_text(json.dumps(self.layout, indent=2), encoding="utf-8")

        promote_character(GENSHIN, "Sandrone", self.layout, self.path)

        self.assertEqual(self._on_disk()[CHARACTER_ALIAS_DISMISSED_KEY][GENSHIN], ["Sandrone"])

    # -- 3. nothing else moves ----------------------------------------------

    def test_the_group_folder_and_its_other_names_survive(self):
        """Promoting one Fatui must not unhook the rest -- the group folder is
        still a real folder on disk holding real files."""
        promote_character(GENSHIN, "Sandrone", self.layout, self.path)

        reloaded = self._on_disk()
        self.assertIn("Fatui", self._roster(reloaded))
        self.assertEqual(self._resolve("Signora", reloaded), "Fatui")
        self.assertEqual(self._resolve("Arlecchino", reloaded), "Fatui")
        self.assertEqual(self._route(_entry(["Signora"]), reloaded).dest_dir,
                         f"{GENSHIN}/Fatui")

    def test_other_franchises_and_the_key_order_are_untouched(self):
        """save_layout re-dumps the whole file with no sort_keys precisely so
        the hand-curated franchise order survives a write."""
        promote_character(GENSHIN, "Sandrone", self.layout, self.path)

        reloaded = self._on_disk()
        self.assertEqual(list(reloaded["franchises"]), list(PROMOTE_LAYOUT["franchises"]))
        self.assertEqual(reloaded["franchises"][HSR], PROMOTE_LAYOUT["franchises"][HSR])
        self.assertEqual(self._aliases(reloaded, HSR), {"Kafka": "Stellaron Hunters"})
        self.assertEqual(reloaded["group_subfolder"], "Others_Group")
        self.assertEqual(reloaded["special_folders"], PROMOTE_LAYOUT["special_folders"])

    # -- 4. no-op calls never rewrite the file -------------------------------

    def test_promote_is_idempotent(self):
        promote_character(GENSHIN, "Sandrone", self.layout, self.path)
        raw, mtime = self._stamp()

        promote_character(GENSHIN, "Sandrone", self.layout, self.path)

        self.assertEqual(self._raw(), raw)
        self.assertEqual(self.path.stat().st_mtime_ns, mtime,
                         "a second promote must not rewrite the file at all")
        self.assertEqual(self._roster().count("Sandrone"), 1)

    def test_promote_of_an_already_promoted_name_is_a_pure_no_op(self):
        """"Hu Tao" is already its own folder with no alias and no pin, so all
        three edits are already satisfied and nothing should be written."""
        raw, mtime = self._stamp()

        promote_character(GENSHIN, "Hu Tao", self.layout, self.path)

        self.assertEqual(self._raw(), raw)
        self.assertEqual(self.path.stat().st_mtime_ns, mtime)
        self.assertEqual(self.layout, PROMOTE_LAYOUT)

    def test_promote_dedupes_a_spacing_or_order_variant_on_the_roster(self):
        """The roster is folder names. "Shinobu Kuki" alongside "Kuki Shinobu"
        would be two folders' worth of intent for one character, and
        resolve_character already treats them as the same name."""
        raw, mtime = self._stamp()

        for variant in ("Kuki Shinobu", "kuki  shinobu", "KUKISHINOBU", "Shinobu Kuki"):
            with self.subTest(variant=variant):
                promote_character(GENSHIN, variant, self.layout, self.path)
                self.assertEqual(self._roster(), PROMOTE_LAYOUT["franchises"][GENSHIN]["characters"])
                self.assertEqual(self._raw(), raw)
                self.assertEqual(self.path.stat().st_mtime_ns, mtime)

    def test_promote_of_a_blank_name_is_a_no_op(self):
        raw, mtime = self._stamp()

        for blank in ("", "   "):
            with self.subTest(name=blank):
                promote_character(GENSHIN, blank, self.layout, self.path)
                self.assertEqual(self._raw(), raw)
                self.assertEqual(self.path.stat().st_mtime_ns, mtime)

    # -- 5. refusals leave the file untouched --------------------------------

    def test_promote_on_an_unknown_franchise_raises_and_writes_nothing(self):
        """Building a roster under a folder that doesn't exist would flag every
        entry that reached it, with no hint of why."""
        raw, mtime = self._stamp()

        with self.assertRaises(ValueError):
            promote_character("No Such Franchise", "Sandrone", self.layout, self.path)

        self.assertEqual(self._raw(), raw)
        self.assertEqual(self.path.stat().st_mtime_ns, mtime)
        self.assertEqual(self.layout, PROMOTE_LAYOUT)

    # -- on-disk bytes -------------------------------------------------------

    def test_non_ascii_name_survives_the_round_trip_and_the_file_stays_lf(self):
        """ensure_ascii=False keeps the name readable in the hand-edited file,
        and newline="" keeps the bytes LF on Windows as well as Linux."""
        name = "Yūjin Kōsaka"

        promote_character(GENSHIN, name, self.layout, self.path)

        self.assertIn(name.encode("utf-8"), self._raw(),
                      "ensure_ascii=False must keep the name unescaped on disk")
        self.assertNotIn(b"\r\n", self._raw())
        self.assertIn(b"\n", self._raw())

        reloaded = self._on_disk()
        self.assertIn(name, self._roster(reloaded))
        self.assertEqual(self._resolve(name, reloaded), name)


class MergeCharacterTest(PromoteMergeTestCase):
    # -- the edit itself -----------------------------------------------------

    def test_merge_adds_the_alias_and_drops_the_roster_entry(self):
        """The inverse of promote: the roster entry has to go, or it would keep
        offering a folder the user just said should not exist."""
        self.assertEqual(self._resolve("Hu Tao"), "Hu Tao")

        merge_character(GENSHIN, "Hu Tao", "Fatui", self.layout, self.path)

        self.assertEqual(self._aliases()["Hu Tao"], "Fatui")
        self.assertNotIn("Hu Tao", self._roster())
        self.assertEqual(self._resolve("Hu Tao"), "Fatui")

        reloaded = self._on_disk()
        self.assertEqual(self._resolve("Hu Tao", reloaded), "Fatui",
                         "the merge did not reach disk")
        self.assertEqual(self._route(_entry(["Hu Tao"]), reloaded).dest_dir,
                         f"{GENSHIN}/Fatui")

    def test_returns_the_updated_layout(self):
        returned = merge_character(GENSHIN, "Hu Tao", "Fatui", self.layout, self.path)
        self.assertIs(returned, self.layout)

    def test_merge_stores_the_configured_spelling_of_the_target(self):
        """The alias value is interpolated straight into a filesystem path, so
        it must be the roster's spelling, never the caller's casing."""
        merge_character(GENSHIN, "Hu Tao", "fatui", self.layout, self.path)

        self.assertEqual(self._aliases()["Hu Tao"], "Fatui")

    def test_merge_of_a_name_that_was_never_on_the_roster(self):
        """The common review-UI case: a typed name that resolves to nothing at
        all. Only the alias half has anything to do."""
        merge_character(GENSHIN, "Il Dottore", "Fatui", self.layout, self.path)

        self.assertEqual(self._resolve("Il Dottore"), "Fatui")
        self.assertEqual(self._roster(), PROMOTE_LAYOUT["franchises"][GENSHIN]["characters"])

    def test_merge_leaves_the_other_franchise_and_the_key_order_alone(self):
        merge_character(GENSHIN, "Hu Tao", "Fatui", self.layout, self.path)

        reloaded = self._on_disk()
        self.assertEqual(list(reloaded["franchises"]), list(PROMOTE_LAYOUT["franchises"]))
        self.assertEqual(reloaded["franchises"][HSR], PROMOTE_LAYOUT["franchises"][HSR])
        self.assertEqual(self._aliases(reloaded, HSR), {"Kafka": "Stellaron Hunters"})

    # -- no-op ---------------------------------------------------------------

    def test_merge_is_idempotent(self):
        merge_character(GENSHIN, "Hu Tao", "Fatui", self.layout, self.path)
        raw, mtime = self._stamp()

        merge_character(GENSHIN, "Hu Tao", "Fatui", self.layout, self.path)

        self.assertEqual(self._raw(), raw)
        self.assertEqual(self.path.stat().st_mtime_ns, mtime,
                         "a second merge must not rewrite the file at all")

    # -- refusals ------------------------------------------------------------

    def test_merge_into_a_non_existent_folder_raises_and_writes_nothing(self):
        """An alias pointing at a folder that doesn't exist resolves to
        nothing, so the entry flags needs_folder at archive time -- which looks,
        from the review UI, exactly like an alias that was never recorded.
        Refusing up front is the only way that failure is visible when caused."""
        raw, mtime = self._stamp()

        with self.assertRaises(ValueError):
            merge_character(GENSHIN, "Hu Tao", "Snezhnaya", self.layout, self.path)

        self.assertEqual(self._raw(), raw)
        self.assertEqual(self.path.stat().st_mtime_ns, mtime)
        self.assertEqual(self.layout, PROMOTE_LAYOUT)

    def test_merge_into_another_franchises_folder_raises(self):
        """The roster is looked up on the resolved franchise, so "Firefly" is
        not a legal target under Genshin even though it is a real folder
        somewhere else."""
        with self.assertRaises(ValueError):
            merge_character(GENSHIN, "Hu Tao", "Firefly", self.layout, self.path)

    def test_merge_of_a_name_into_itself_raises_and_writes_nothing(self):
        """_roster_drop would delete the very entry the new alias points at,
        leaving an alias to a folder that no longer exists."""
        raw, mtime = self._stamp()

        with self.assertRaises(ValueError):
            merge_character(GENSHIN, "hu-tao", "Hu Tao", self.layout, self.path)

        self.assertEqual(self._raw(), raw)
        self.assertEqual(self.path.stat().st_mtime_ns, mtime)
        self.assertEqual(self.layout, PROMOTE_LAYOUT)

    def test_merge_on_an_unknown_franchise_raises_and_writes_nothing(self):
        raw, mtime = self._stamp()

        with self.assertRaises(ValueError):
            merge_character("No Such Franchise", "Hu Tao", "Fatui", self.layout, self.path)

        self.assertEqual(self._raw(), raw)
        self.assertEqual(self.path.stat().st_mtime_ns, mtime)
        self.assertEqual(self.layout, PROMOTE_LAYOUT)


class PromoteMergeRoundTripTest(PromoteMergeTestCase):
    def test_promote_then_merge_back_restores_routing(self):
        """Asserted as ROUTING, not as byte-identity: the roster legitimately
        comes back in a different order (the promoted name is appended, then
        removed from wherever it landed), and pinning bytes here would fail on
        a perfectly correct result."""
        entry = _entry(["Sandrone"])
        before = self._route(entry).dest_dir
        self.assertEqual(before, f"{GENSHIN}/Fatui")

        promote_character(GENSHIN, "Sandrone", self.layout, self.path)
        self.assertEqual(self._route(entry).dest_dir, f"{GENSHIN}/Sandrone")

        merge_character(GENSHIN, "Sandrone", "Fatui", self.layout, self.path)

        reloaded = self._on_disk()
        self.assertEqual(self._route(entry, reloaded).dest_dir, before)
        self.assertEqual(self._resolve("Sandrone", reloaded), "Fatui")
        self.assertNotIn("Sandrone", self._roster(reloaded))
        self.assertEqual(sorted(self._roster(reloaded)),
                         sorted(PROMOTE_LAYOUT["franchises"][GENSHIN]["characters"]))
        self.assertEqual(self._aliases(reloaded), PROMOTE_LAYOUT["character_aliases"][GENSHIN])

    def test_merge_then_promote_back_restores_routing(self):
        """The other direction, which is the one the review UI actually runs
        when a merge is undone."""
        entry = _entry(["Hu Tao"])
        before = self._route(entry).dest_dir

        merge_character(GENSHIN, "Hu Tao", "Fatui", self.layout, self.path)
        self.assertEqual(self._route(entry).dest_dir, f"{GENSHIN}/Fatui")

        promote_character(GENSHIN, "Hu Tao", self.layout, self.path)

        reloaded = self._on_disk()
        self.assertEqual(self._route(entry, reloaded).dest_dir, before)
        self.assertEqual(self._aliases(reloaded), PROMOTE_LAYOUT["character_aliases"][GENSHIN])


if __name__ == "__main__":
    unittest.main(verbosity=2)
