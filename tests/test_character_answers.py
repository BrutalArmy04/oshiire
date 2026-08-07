"""Regression tests for the two persistent answers to the character-alias prompt.

Declining that prompt used to write nothing, so `_character_alias_candidate`
re-offered the same unmatched name on every image that carried it, forever.
The fix is two optional top-level layout.json tables --
`character_group_route` (always file this name to the group subfolder) and
`character_alias_dismissed` (stop prompting, no routing effect) -- plus four
writers and two lookups in shortname.py, and one new check in archive.py's
nested branch.

What these pin down, in the order the bugs would bite:

  1. The round trip persists TO DISK. A writer that mutated only the passed-in
     dict would satisfy a naive in-memory assertion and change nothing on disk,
     which is exactly the bug being fixed (an answer that isn't remembered).
  2. Anything settable is retractable, by any spelling that would have set it.
     Both tables match on resolve_character's rule -- normalize_name_key plus a
     reversed pass for two-token names -- and the remove_* functions have to
     use the SAME rule, or an answer given as "Hu Tao" and read back as "hutao"
     could be found but never taken back.
  3. Absent keys are invisible. Both tables are optional and there is no
     migration, so every lookup and every remove must tolerate a layout.json
     that has neither key, and a layout with neither must route identically to
     before they existed.
  4. Re-answering doesn't grow duplicates, and doesn't rewrite the file.
     Asserted on the file BYTES, since a no-op rewrite would leave the parsed
     dict equal while churning mtime and possibly reordering.
  5. In archive.py: a group route beats a roster match. That ordering is the
     one deliberate precedence decision here -- an explicit user directive
     outranks an incidental same-named subfolder -- and it is invisible unless
     a test sets up a name that is BOTH.
  6. A dismissal has no routing effect whatsoever. It is review-side only, and
     a nested franchise without `"fallback": "root"` must still flag it.

Runs entirely on synthetic layouts in a temp dir. Neither the real layout.json
nor ARCHIVE_DIR is read or written.

    python -m unittest discover -s tests
    python tests/test_character_answers.py
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
    is_alias_dismissed,
    is_group_routed,
    load_layout,
    remove_character_alias_dismissal,
    remove_character_group_route,
    save_character_alias_dismissal,
    save_character_group_route,
)

FOLDER = "Starfall Chronicle"

BASE_LAYOUT = {
    "group_subfolder": "Others_Group",
    "special_folders": {
        "crossover": "Crossover",
        "others_oc": "Others/Artist's Original",
        "others_unknown_source": "Others/Unknown Sauce",
        "others_known_series": "Others/Known Series",
    },
    "shortname_file": "known_series_names.txt",
    "franchise_aliases": {},
    "character_aliases": {},
    "franchises": {
        FOLDER: {
            "style": "nested",
            # "Kestrel" is deliberately BOTH a roster folder and (in one test) a
            # group-routed name -- that overlap is where the precedence lives.
            "characters": ["Sera", "Kestrel", "Shinobu Kuki"],
        },
        "Lantern District": {
            "style": "nested",
            "characters": ["Yuzu Hoshimi"],
            "fallback": "root",
        },
    },
}


class LayoutFileTestCase(unittest.TestCase):
    """A throwaway layout.json every writer test writes through."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="oshiire-charanswers-"))
        self.path = self.tmp / "layout.json"
        self.layout = copy.deepcopy(BASE_LAYOUT)
        self.path.write_text(json.dumps(self.layout, indent=2), encoding="utf-8")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _on_disk(self) -> dict:
        return load_layout(self.path)

    def _raw(self) -> bytes:
        return self.path.read_bytes()


# ---------------------------------------------------------------------------
# The six shortname.py functions. Both tables have identical mechanics, so each
# behaviour is asserted against BOTH via the (save, remove, query) triples --
# a fix applied to one and forgotten on the other is the likely regression.
# ---------------------------------------------------------------------------

TABLES = (
    ("character_group_route", save_character_group_route,
     remove_character_group_route, is_group_routed),
    ("character_alias_dismissed", save_character_alias_dismissal,
     remove_character_alias_dismissal, is_alias_dismissed),
)


class CharacterAnswerStoreTest(LayoutFileTestCase):
    def test_absent_key_answers_false_and_is_not_created_by_reading(self):
        """Both tables are optional with no migration: a layout that has never
        been written to must read cleanly and must not sprout empty keys."""
        for key, _save, _remove, query in TABLES:
            with self.subTest(table=key):
                self.assertFalse(query(FOLDER, "Anyone", self.layout))
                self.assertFalse(query("No Such Folder", "Anyone", self.layout))
                self.assertNotIn(key, self.layout)
        # ...and neither does an empty dict, or None-ish input.
        self.assertFalse(is_group_routed(FOLDER, "Anyone", {}))
        self.assertFalse(is_alias_dismissed(FOLDER, "", self.layout))

    def test_round_trip_persists_to_disk(self):
        """The whole point: an answer given once is still there next launch."""
        for key, save, _remove, query in TABLES:
            with self.subTest(table=key):
                save(FOLDER, "The Quinn Twins", self.layout, self.path)
                self.assertTrue(query(FOLDER, "The Quinn Twins", self.layout))
                reloaded = self._on_disk()
                self.assertEqual(reloaded[key][FOLDER], ["The Quinn Twins"])
                self.assertTrue(query(FOLDER, "The Quinn Twins", reloaded))

    def test_remove_round_trip_restores_the_original_file(self):
        """Retracting the only answer prunes the emptied folder entry and the
        emptied table, so layout.json goes back to exactly what it was."""
        for key, save, remove, query in TABLES:
            with self.subTest(table=key):
                before = self._raw()
                save(FOLDER, "The Quinn Twins", self.layout, self.path)
                self.assertNotEqual(self._raw(), before)
                remove(FOLDER, "The Quinn Twins", self.layout, self.path)
                self.assertFalse(query(FOLDER, "The Quinn Twins", self.layout))
                self.assertNotIn(key, self.layout)
                self.assertNotIn(key, self._on_disk())
                self.assertEqual(json.loads(self._raw()), json.loads(before))

    def test_remove_keeps_the_other_names_and_the_other_folders(self):
        for key, save, remove, query in TABLES:
            with self.subTest(table=key):
                save(FOLDER, "The Quinn Twins", self.layout, self.path)
                save(FOLDER, "Unnamed Barkeep", self.layout, self.path)
                save("Lantern District", "The Quinn Twins", self.layout, self.path)
                remove(FOLDER, "The Quinn Twins", self.layout, self.path)

                reloaded = self._on_disk()
                self.assertEqual(reloaded[key][FOLDER], ["Unnamed Barkeep"])
                self.assertEqual(reloaded[key]["Lantern District"], ["The Quinn Twins"])
                self.assertTrue(query("Lantern District", "The Quinn Twins", reloaded))
                self.assertFalse(query(FOLDER, "The Quinn Twins", reloaded))

    def test_spacing_and_casing_variants_resolve_to_one_answer(self):
        """The name is stored as the prompt showed it, but the NEXT post can
        spell it any way -- so the lookup has to use normalize_name_key, the
        same key resolve_character compares on."""
        for key, save, remove, query in TABLES:
            with self.subTest(table=key):
                save(FOLDER, "Hu Tao", self.layout, self.path)
                for variant in ("Hu Tao", "hu tao", "HUTAO", "hu-tao", "Hu  Tao", "hu.tao"):
                    self.assertTrue(query(FOLDER, variant, self.layout), variant)
                self.assertFalse(query(FOLDER, "Hutao Zhu", self.layout))
                # ...and it is retractable by any of them, not just the stored one.
                remove(FOLDER, "hu-tao", self.layout, self.path)
                self.assertFalse(query(FOLDER, "Hu Tao", self.layout))

    def test_two_token_names_match_in_reversed_order(self):
        """resolve_character retries a two-token name swapped, so these tables
        must too -- otherwise an answer given for "Kuki Shinobu" is re-prompted
        the moment a post tags it "Shinobu Kuki"."""
        for key, save, remove, query in TABLES:
            with self.subTest(table=key):
                save(FOLDER, "Kuki Shinobu", self.layout, self.path)
                self.assertTrue(query(FOLDER, "Shinobu Kuki", self.layout))
                self.assertTrue(query(FOLDER, "shinobu  KUKI", self.layout))
                # Reversed AND run together is NOT a match, and shouldn't be:
                # reversed_name_variant splits on whitespace, so with no token
                # boundary there is nothing to reverse. Exactly the limit
                # resolve_character has -- which is the point. These tables
                # match on its rule, not on a looser one of their own.
                self.assertFalse(query(FOLDER, "shinobukuki", self.layout))
                self.assertFalse(query(FOLDER, "shinobu-kuki", self.layout))
                remove(FOLDER, "Shinobu Kuki", self.layout, self.path)
                self.assertFalse(query(FOLDER, "Kuki Shinobu", self.layout))

    def test_three_token_names_are_not_permuted(self):
        """The two-token cap is deliberate: with 3+ tokens the permutations
        stop being a name-order question and start being guesses."""
        for key, save, _remove, query in TABLES:
            with self.subTest(table=key):
                save(FOLDER, "Ines Adler Voss", self.layout, self.path)
                self.assertTrue(query(FOLDER, "Ines Adler Voss", self.layout))
                self.assertFalse(query(FOLDER, "Voss Adler Ines", self.layout))

    def test_repeat_save_is_a_no_op_not_a_duplicate(self):
        """Answering the same prompt twice (two images, same name, the second
        arriving before the first was reloaded) must not append a second row --
        and must not rewrite the file at all."""
        for key, save, _remove, query in TABLES:
            with self.subTest(table=key):
                save(FOLDER, "The Quinn Twins", self.layout, self.path)
                after_first = self._raw()
                save(FOLDER, "The Quinn Twins", self.layout, self.path)
                save(FOLDER, "the quinn twins", self.layout, self.path)
                save(FOLDER, "TheQuinnTwins", self.layout, self.path)
                self.assertEqual(self.layout[key][FOLDER], ["The Quinn Twins"])
                self.assertEqual(self._raw(), after_first)

    def test_removing_an_absent_name_does_not_rewrite_the_file(self):
        for key, save, remove, query in TABLES:
            with self.subTest(table=key):
                save(FOLDER, "The Quinn Twins", self.layout, self.path)
                after_save = self._raw()
                remove(FOLDER, "Someone Else", self.layout, self.path)
                remove("No Such Folder", "The Quinn Twins", self.layout, self.path)
                self.assertEqual(self._raw(), after_save)
                self.assertTrue(query(FOLDER, "The Quinn Twins", self.layout))

    def test_removing_from_a_layout_with_neither_key_is_a_clean_no_op(self):
        before = self._raw()
        remove_character_group_route(FOLDER, "Anyone", self.layout, self.path)
        remove_character_alias_dismissal(FOLDER, "Anyone", self.layout, self.path)
        self.assertEqual(self._raw(), before)
        self.assertEqual(self.layout, BASE_LAYOUT)

    def test_the_two_tables_are_independent(self):
        """They answer different questions -- one routes, one only silences --
        so setting one must never imply the other."""
        save_character_group_route(FOLDER, "The Quinn Twins", self.layout, self.path)
        save_character_alias_dismissal(FOLDER, "Unnamed Barkeep", self.layout, self.path)
        self.assertTrue(is_group_routed(FOLDER, "The Quinn Twins", self.layout))
        self.assertFalse(is_alias_dismissed(FOLDER, "The Quinn Twins", self.layout))
        self.assertTrue(is_alias_dismissed(FOLDER, "Unnamed Barkeep", self.layout))
        self.assertFalse(is_group_routed(FOLDER, "Unnamed Barkeep", self.layout))

    def test_character_aliases_is_left_alone(self):
        """The new answers live in their own top-level keys precisely so the
        variant -> canonical identity map stays an identity map -- no sentinel
        values, no nulls, nothing for a Settings panel to have to special-case."""
        save_character_group_route(FOLDER, "The Quinn Twins", self.layout, self.path)
        save_character_alias_dismissal(FOLDER, "Unnamed Barkeep", self.layout, self.path)
        self.assertEqual(self._on_disk()["character_aliases"], {})


# ---------------------------------------------------------------------------
# archive.py routing
# ---------------------------------------------------------------------------

def _entry(characters, franchise=FOLDER, **extra):
    entry = {
        "post_id": "t3_test",
        "title": "Test",
        "franchise": [franchise],
        "character_guess": list(characters),
        "crossover": False,
    }
    entry.update(extra)
    return entry


class GroupRouteRoutingTest(LayoutFileTestCase):
    def _route(self, entry, layout=None):
        return archive.route_entry(entry, layout or self.layout, [], None)

    def test_layout_with_neither_key_routes_exactly_as_before(self):
        """The no-migration guarantee, asserted as routing rather than as
        config parsing: the whole nested ladder must be untouched."""
        cases = [
            (_entry(["Sera"]), "move", f"{FOLDER}/Sera"),
            (_entry(["Sera", "Kestrel"]), "move", f"{FOLDER}/Others_Group"),
            (_entry(["Sera"], same_series_group=True), "move", f"{FOLDER}/Others_Group"),
            (_entry(["Nobody At All"]), "flag", None),
            (_entry([]), "flag", None),
            (_entry(["Nobody"], franchise="Lantern District"), "move", "Lantern District"),
        ]
        for entry, action, dest in cases:
            with self.subTest(characters=entry["character_guess"], franchise=entry["franchise"]):
                result = self._route(entry)
                self.assertEqual(result.action, action)
                if dest is not None:
                    self.assertEqual(result.dest_dir, dest)

    def test_group_routed_character_goes_to_the_group_folder(self):
        """The main case: a single unmatched name that would otherwise flag."""
        entry = _entry(["The Quinn Twins"])
        self.assertEqual(self._route(entry).action, "flag")
        save_character_group_route(FOLDER, "The Quinn Twins", self.layout, self.path)
        result = self._route(entry)
        self.assertEqual(result.action, "move")
        self.assertEqual(result.dest_dir, f"{FOLDER}/Others_Group")

    def test_group_route_uses_the_configured_group_subfolder(self):
        """Read from layout['group_subfolder'], never hardcoded."""
        self.layout["group_subfolder"] = "Ensemble"
        save_character_group_route(FOLDER, "The Quinn Twins", self.layout, self.path)
        self.assertEqual(self._route(_entry(["The Quinn Twins"])).dest_dir, f"{FOLDER}/Ensemble")

    def test_group_route_beats_a_roster_match(self):
        """The precedence decision. "Kestrel" IS a real subfolder here, so
        without the check ordering this would file into it -- an explicit user
        directive has to outrank an incidental roster hit, and the Settings
        panel is where it gets retracted."""
        self.assertEqual(self._route(_entry(["Kestrel"])).dest_dir, f"{FOLDER}/Kestrel")
        save_character_group_route(FOLDER, "Kestrel", self.layout, self.path)
        self.assertEqual(self._route(_entry(["Kestrel"])).dest_dir, f"{FOLDER}/Others_Group")

    def test_group_route_beats_the_root_fallback(self):
        """`fallback: "root"` is the franchise's blanket answer for unmatched
        names; a per-name answer is more specific and must win."""
        entry = _entry(["The Quinn Twins"], franchise="Lantern District")
        self.assertEqual(self._route(entry).dest_dir, "Lantern District")
        save_character_group_route("Lantern District", "The Quinn Twins", self.layout, self.path)
        self.assertEqual(self._route(entry).dest_dir, "Lantern District/Others_Group")

    def test_group_route_matches_a_spelling_variant(self):
        save_character_group_route(FOLDER, "The Quinn Twins", self.layout, self.path)
        for variant in ("the quinn twins", "TheQuinnTwins", "The-Quinn-Twins"):
            with self.subTest(variant=variant):
                self.assertEqual(self._route(_entry([variant])).dest_dir, f"{FOLDER}/Others_Group")

    def test_group_route_is_scoped_to_its_franchise(self):
        """Keyed by folder for the same reason character_aliases is: two
        franchises can have different characters sharing one short name."""
        save_character_group_route("Lantern District", "The Quinn Twins", self.layout, self.path)
        self.assertEqual(self._route(_entry(["The Quinn Twins"])).action, "flag")

    def test_two_group_routed_names_still_reach_the_same_group_folder(self):
        """distinct_characters keys on the resolved subfolder, so two unmatched
        names stay two identities and take the len >= 2 branch -- same
        destination, so the ordering introduces no dedup hazard."""
        save_character_group_route(FOLDER, "The Quinn Twins", self.layout, self.path)
        save_character_group_route(FOLDER, "Unnamed Barkeep", self.layout, self.path)
        result = self._route(_entry(["The Quinn Twins", "Unnamed Barkeep"]))
        self.assertEqual(result.dest_dir, f"{FOLDER}/Others_Group")

    def test_group_route_does_not_outrank_crossover_or_an_override(self):
        """Precedence 1 and the archive_override branches are above the nested
        ladder entirely; a per-character answer must not reach past them."""
        save_character_group_route(FOLDER, "The Quinn Twins", self.layout, self.path)
        crossover = _entry(["The Quinn Twins"], crossover=True)
        self.assertEqual(self._route(crossover).dest_dir, "Crossover")
        oc = _entry(["The Quinn Twins"], archive_override="artist_original")
        self.assertEqual(self._route(oc).dest_dir, "Others/Artist's Original")

    def test_group_route_does_not_apply_to_a_flat_franchise(self):
        """A flat franchise has no character folders and no group folder -- the
        name never reaches the path, so the answer must be inert there."""
        self.layout["franchises"]["Neon Ward"] = {"style": "flat"}
        save_character_group_route("Neon Ward", "The Quinn Twins", self.layout, self.path)
        result = self._route(_entry(["The Quinn Twins"], franchise="Neon Ward"))
        self.assertEqual(result.dest_dir, "Neon Ward")

    def test_dismissed_name_still_flags_needs_folder(self):
        """A dismissal is review-side ONLY. It silences the prompt; it does not
        decide where the image files, and archive.py must not read it as an
        answer to that question."""
        save_character_alias_dismissal(FOLDER, "The Quinn Twins", self.layout, self.path)
        result = self._route(_entry(["The Quinn Twins"]))
        self.assertEqual(result.action, "flag")
        self.assertEqual(result.flag_reason, "needs_folder")
        self.assertIn("The Quinn Twins", result.flag_detail)

    def test_dismissed_name_on_a_root_fallback_franchise_still_falls_back(self):
        save_character_alias_dismissal("Lantern District", "The Quinn Twins", self.layout, self.path)
        entry = _entry(["The Quinn Twins"], franchise="Lantern District")
        self.assertEqual(self._route(entry).dest_dir, "Lantern District")

    def test_dismissal_does_not_disturb_a_matching_character(self):
        save_character_alias_dismissal(FOLDER, "Sera", self.layout, self.path)
        self.assertEqual(self._route(_entry(["Sera"])).dest_dir, f"{FOLDER}/Sera")


if __name__ == "__main__":
    unittest.main(verbosity=2)
