"""Resolve: "Map alias to selected character" on an entry with NO character tag.

A needs_folder flag of "No subfolder for an untagged image in <Franchise>" has an
empty `character_guess`, so there is no name to alias. The button used to answer
"This entry has no character to alias." and do nothing. It now files THIS image
under the selected existing character: `character_guess` becomes [selected],
no alias is written, and Undo restores the previous list exactly.

Driven through the real resolve handlers in a private sandbox (tests/
review_sandbox.py): a temp data root with a synthetic manifest/layout and a temp
archive folder. The real manifest.json, layout.json and ARCHIVE_DIR are never
read or written, and nothing here touches the network.

    python -m unittest tests.test_resolve_untagged
"""
import copy
import json
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from review_sandbox import ReviewSandbox  # noqa: E402

import archive  # noqa: E402  (binds nothing to the cwd at import)
import shortname  # noqa: E402

FOLDER = "Genshin Impact"

LAYOUT = {
    "group_subfolder": "Others_Group",
    "special_folders": {
        "crossover": "Crossover", "wallpaper_root": "Wallpaper",
        "wallpaper_pc": "PC", "wallpaper_phone": "Phone",
        "others_oc": "Others/Artist's Original",
        "others_unknown_source": "Others/Unknown Sauce",
        "others_known_series": "Others/Known Series",
    },
    "shortname_file": "known_series_names.txt",
    "franchise_aliases": {},
    "character_aliases": {},
    "franchises": {FOLDER: {"style": "nested", "characters": ["Ayaka", "NPCs"]}},
}


def _entry(post_id, staging, characters, **extra):
    path = staging / f"{post_id}.jpg"
    path.write_bytes(b"not really a jpeg")
    entry = {
        "post_id": post_id,
        "title": f"Image {post_id}",
        "subreddit": "TestSub",
        "permalink": f"https://www.reddit.com/r/TestSub/comments/{post_id[3:]}/",
        "image_url": f"https://i.redd.it/{post_id}.jpg",
        "local_path": str(path).replace("\\", "/"),
        "fetched_at": "2026-01-01T00:00:00+00:00",
        "status": "approved",
        "franchise": [FOLDER],
        "guess_confidence": "low",
        "guess_source": "title",
        "crossover": False,
    }
    if characters is not None:
        entry["character_guess"] = characters
    entry.update(extra)
    return entry


class UntaggedCharacterTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._tmp = Path(tempfile.mkdtemp(prefix="oshiire-resolve-untagged-"))
        cls._archive = Path(tempfile.mkdtemp(prefix="oshiire-resolve-archive-"))
        for sub in ("NPCs", "Ayaka", "Others_Group"):
            (cls._archive / FOLDER / sub).mkdir(parents=True)
        staging = cls._tmp / "staging"
        staging.mkdir()
        shutil.copy(REPO / "examples" / "known_series_names.example.txt",
                    cls._tmp / "known_series_names.txt")
        shutil.copy(REPO / "examples" / "subreddit_map.example.json",
                    cls._tmp / "subreddit_map.json")
        (cls._tmp / "layout.json").write_text(json.dumps(LAYOUT, indent=2), encoding="utf-8")
        cls._manifest = {
            "t3_empty": _entry("t3_empty", staging, []),        # key present, empty list
            "t3_absent": _entry("t3_absent", staging, None),    # key absent altogether
            "t3_tagged": _entry("t3_tagged", staging, ["Aya"]),  # a real, unmatched tag
        }
        (cls._tmp / "manifest.json").write_text(json.dumps(cls._manifest, indent=2),
                                                encoding="utf-8")
        cls._sandbox = ReviewSandbox(cls._tmp, env={"ARCHIVE_DIR": str(cls._archive)})
        review = cls._sandbox.activate()
        cls.resolve = review.resolve

    @classmethod
    def tearDownClass(cls):
        cls._sandbox.deactivate()
        shutil.rmtree(cls._tmp, ignore_errors=True)
        shutil.rmtree(cls._archive, ignore_errors=True)

    def setUp(self):
        resolve = self.resolve
        self.addCleanup(self._restore_disk)
        resolve.reload_state()

    def _restore_disk(self):
        (self._tmp / "manifest.json").write_text(json.dumps(self._manifest, indent=2),
                                                 encoding="utf-8")
        (self._tmp / "layout.json").write_text(json.dumps(LAYOUT, indent=2), encoding="utf-8")

    def _show(self, post_id):
        """Point the resolve cursor at `post_id` (it must be in the flagged queue)."""
        resolve = self.resolve
        resolve.current_index = next(i for i, r in enumerate(resolve.rows)
                                     if r["post_id"] == post_id)

    def _disk_manifest(self):
        return json.loads((self._tmp / "manifest.json").read_text(encoding="utf-8"))

    def _disk_layout(self):
        return json.loads((self._tmp / "layout.json").read_text(encoding="utf-8"))

    def _route(self, post_id):
        resolve = self.resolve
        return archive.route_entry(resolve.manifest[post_id], resolve.layout,
                                   resolve.shortname_entries, resolve.series_aliases)

    # -- the setup ---------------------------------------------------------------
    def test_the_untagged_entries_are_flagged_needs_folder_untagged(self):
        flagged = {r["post_id"]: r for r in self.resolve.rows}
        for post_id in ("t3_empty", "t3_absent"):
            self.assertEqual(flagged[post_id]["flag_reason"], "needs_folder")
            self.assertIn("an untagged image", flagged[post_id]["flag_detail"])

    # -- the fix -----------------------------------------------------------------
    def test_untagged_files_under_the_selected_character_and_writes_no_alias(self):
        resolve = self.resolve
        layout_before = copy.deepcopy(resolve.layout)
        layout_file_before = (self._tmp / "layout.json").read_text(encoding="utf-8")
        self._show("t3_empty")
        out = resolve.on_map_character_alias("NPCs")

        self.assertEqual(resolve.manifest["t3_empty"]["character_guess"], ["NPCs"])
        self.assertEqual(self._disk_manifest()["t3_empty"]["character_guess"], ["NPCs"])
        self.assertEqual(resolve.layout, layout_before)                      # no alias, no change
        self.assertEqual((self._tmp / "layout.json").read_text(encoding="utf-8"),
                         layout_file_before)                                 # layout.json untouched
        self.assertNotIn("t3_empty", {r["post_id"] for r in resolve.rows})   # resolved
        self.assertTrue(out[0].startswith("Resolved."))
        self.assertEqual(resolve.last_action["kind"], "manifest_field")
        self.assertEqual(resolve.last_action["field"], "character_guess")

    def test_it_then_routes_to_the_franchise_character_subfolder(self):
        self._show("t3_empty")
        self.resolve.on_map_character_alias("NPCs")
        result = self._route("t3_empty")
        self.assertEqual((result.action, result.dest_dir), ("move", f"{FOLDER}/NPCs"))

        plan, _ = archive.plan_moves(self.resolve.manifest, self.resolve.layout,
                                     self.resolve.shortname_entries, self._archive,
                                     self.resolve.series_aliases)
        row = next(r for r in plan if r["post_id"] == "t3_empty")
        self.assertEqual(row["outcome"], "move")

    def test_an_entry_without_the_key_works_the_same(self):
        self._show("t3_absent")
        self.resolve.on_map_character_alias("Ayaka")
        self.assertEqual(self.resolve.manifest["t3_absent"]["character_guess"], ["Ayaka"])
        self.assertEqual(self._route("t3_absent").dest_dir, f"{FOLDER}/Ayaka")

    def test_undo_restores_an_empty_list_not_a_missing_key(self):
        self._show("t3_empty")
        self.resolve.on_map_character_alias("NPCs")
        out = self.resolve.on_undo()
        entry = self.resolve.manifest["t3_empty"]
        self.assertIn("character_guess", entry)
        self.assertEqual(entry["character_guess"], [])
        self.assertEqual(self._disk_manifest()["t3_empty"]["character_guess"], [])
        self.assertEqual(out[0], "Undone.")
        flagged = {r["post_id"] for r in self.resolve.rows}
        self.assertIn("t3_empty", flagged)                                   # flagged again

    def test_undo_restores_an_absent_key_as_absent(self):
        self._show("t3_absent")
        self.resolve.on_map_character_alias("Ayaka")
        self.resolve.on_undo()
        self.assertNotIn("character_guess", self.resolve.manifest["t3_absent"])
        self.assertNotIn("character_guess", self._disk_manifest()["t3_absent"])

    def test_nothing_selected_changes_nothing(self):
        self._show("t3_empty")
        out = self.resolve.on_map_character_alias(None)
        self.assertEqual(out[0], "Pick a character first.")
        self.assertEqual(self.resolve.manifest["t3_empty"]["character_guess"], [])
        self.assertIsNone(self.resolve.last_action)

    def test_an_unresolved_franchise_still_refuses(self):
        self._show("t3_empty")
        resolve = self.resolve
        resolve.manifest["t3_empty"]["franchise"] = ["Nowhere At All"]
        try:
            out = resolve.on_map_character_alias("NPCs")
        finally:
            resolve.manifest["t3_empty"]["franchise"] = [FOLDER]
        self.assertEqual(resolve.manifest["t3_empty"]["character_guess"], [])
        self.assertIn("Franchise isn't resolved", out[0])

    # -- the rules that could intercept it ---------------------------------------
    def test_fallback_root_cannot_intercept_it(self):
        """fallback:root only applies when NO subfolder matched; a matched
        character is routed first."""
        resolve = self.resolve
        resolve.layout["franchises"][FOLDER]["fallback"] = "root"
        try:
            resolve.manifest["t3_empty"]["character_guess"] = ["NPCs"]
            self.assertEqual(self._route("t3_empty").dest_dir, f"{FOLDER}/NPCs")
        finally:
            resolve.layout["franchises"][FOLDER].pop("fallback", None)
            resolve.manifest["t3_empty"]["character_guess"] = []

    def test_a_recorded_group_route_for_that_character_does_intercept_it(self):
        """By design (an explicit "always file this name to the group folder"
        directive outranks the roster): documented here so a surprise is not one."""
        resolve = self.resolve
        resolve.layout[shortname.CHARACTER_GROUP_ROUTE_KEY] = {FOLDER: ["NPCs"]}
        try:
            resolve.manifest["t3_empty"]["character_guess"] = ["NPCs"]
            self.assertEqual(self._route("t3_empty").dest_dir, f"{FOLDER}/Others_Group")
        finally:
            resolve.layout.pop(shortname.CHARACTER_GROUP_ROUTE_KEY, None)
            resolve.manifest["t3_empty"]["character_guess"] = []

    # -- the tagged path is unchanged --------------------------------------------
    def test_a_tagged_entry_still_writes_the_alias_and_keeps_its_tag(self):
        resolve = self.resolve
        self._show("t3_tagged")
        out = resolve.on_map_character_alias("Ayaka")
        self.assertEqual(self._disk_layout()["character_aliases"][FOLDER]["Aya"], "Ayaka")
        self.assertEqual(resolve.manifest["t3_tagged"]["character_guess"], ["Aya"])  # untouched
        self.assertEqual(resolve.last_action["kind"], "layout")
        self.assertTrue(out[0].startswith("Resolved."))
        self.assertEqual(self._route("t3_tagged").dest_dir, f"{FOLDER}/Ayaka")

    def test_a_tagged_undo_still_reverts_the_layout(self):
        resolve = self.resolve
        self._show("t3_tagged")
        resolve.on_map_character_alias("Ayaka")
        resolve.on_undo()
        self.assertNotIn("Aya", self._disk_layout().get("character_aliases", {}).get(FOLDER, {}))

    # -- the control's label ------------------------------------------------------
    def _button_update(self, post_id):
        self._show(post_id)
        return self.resolve._render_current()[18]       # map_character_btn's slot

    def test_the_button_is_shown_and_relabelled_for_an_untagged_entry(self):
        update = self._button_update("t3_empty")
        self.assertTrue(update["visible"])
        self.assertEqual(update["value"], "File under selected character")

    def test_the_button_keeps_its_label_for_a_tagged_entry(self):
        update = self._button_update("t3_tagged")
        self.assertTrue(update["visible"])
        self.assertEqual(update["value"], "Map alias to selected character")


if __name__ == "__main__":
    unittest.main()
