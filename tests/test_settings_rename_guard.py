"""Tests for the Settings-panel rename guard (all five sub-panels).

The bug: with an entry picked, typing a different name and clicking Save
RENAMED the picked entry (remove + re-add), and a rename onto an existing name
silently overwrote it. The fix makes the Save button's label track what the
click will do (_form_intent) and makes the save handler refuse any rename whose
button label isn't the one the user actually clicked (shown_label).

Two layers, same harness as test_settings_config_editors.py:

  * _form_intent -- the pure intent helper, one table test per identity rule.
  * The five panel handlers (SettingsRenameGuardTest), driven against a
    SYNTHETIC layout/series/shortname/subreddit-map tree in a temp cwd. The
    real gitignored stores are never read or written.

    python -m unittest tests.test_settings_rename_guard
"""
import copy
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
sys.path.insert(0, str(REPO / "src"))

from review_sandbox import ReviewSandbox  # noqa: E402


PANEL_LAYOUT = {
    "archive_dir_env": "ARCHIVE_DIR",
    "group_subfolder": "Others_Group",
    "special_folders": {
        "crossover": "Crossover",
        "wallpaper_root": "Wallpaper", "wallpaper_pc": "PC", "wallpaper_phone": "Phone",
        "others_oc": "Others/Artist's Original",
        "others_unknown_source": "Others/Unknown Sauce",
        "others_known_series": "Others/Known Series",
    },
    "shortname_file": "known_series_names.txt",
    "franchise_aliases": {"Azure Lane": "Azur Lane"},
    "character_aliases": {"Genshin Impact": {"Raiden Shogun": "Raiden"}},
    "franchises": {
        "Genshin Impact": {"style": "nested", "characters": ["Raiden", "Hu Tao"]},
        "Azur Lane": {"style": "flat"},
    },
}

SHORTNAME_TEXT = "# Known series shortnames\nNK = NIKKE The Goddess of Victory\n"
SERIES_ALIASES = {"_comment": "hand-written note", "aliases": {"Re Zero": "Re:Zero"}}


def _build_panel_sandbox(tmp: Path) -> None:
    (tmp / "layout.json").write_text(json.dumps(PANEL_LAYOUT, indent=2), encoding="utf-8")
    (tmp / "known_series_names.txt").write_text(SHORTNAME_TEXT, encoding="utf-8")
    (tmp / "data").mkdir()
    (tmp / "data" / "series_aliases.json").write_text(
        json.dumps(SERIES_ALIASES, indent=2), encoding="utf-8"
    )
    shutil.copy(REPO / "examples" / "subreddit_map.example.json", tmp / "subreddit_map.json")
    (tmp / "staging").mkdir()
    (tmp / "manifest.json").write_text("{}", encoding="utf-8")


# ---------------------------------------------------------------------------
# Part 1 -- _form_intent, the pure decision helper. No panel state needed.
# ---------------------------------------------------------------------------
class FormIntentTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._tmp = tempfile.mkdtemp(prefix="oshiire-intent-")
        _build_panel_sandbox(Path(cls._tmp))
        # A private review copy bound to this sandbox (cwd, scrubbed env), whatever
        # order the test modules load in -- see tests/review_sandbox.py.
        cls._sandbox = ReviewSandbox(cls._tmp)
        cls.review = review = cls._sandbox.activate()

    @classmethod
    def tearDownClass(cls):
        cls._sandbox.deactivate()
        shutil.rmtree(cls._tmp, ignore_errors=True)

    def _check(self, same, noun, existing, cases):
        review = self.review
        for selected, key, expected in cases:
            with self.subTest(same=noun, selected=selected, key=key):
                intent, label, variant, interactive = review._form_intent(
                    selected, key, existing, same, noun)
                self.assertEqual(
                    (intent, label, variant, interactive), expected)

    def test_series_identity_rule_all_five_intents(self):
        review = self.review
        same, noun = review.same_series_name, "franchise alias"
        existing = ["Azure Lane", "Genshin"]
        self._check(same, noun, existing, [
            (None, "Blue Archive",
             ("new", "Save new franchise alias", "primary", True)),
            # spelling-only match (case) -> exists, not a second key
            (None, "azure lane",
             ("exists", "“Azure Lane” already exists — pick it above to edit",
              "secondary", False)),
            ("Azure Lane", "azure lane",
             ("update", "Save changes", "primary", True)),
            ("Azure Lane", "",
             ("update", "Save changes", "primary", True)),
            ("Azure Lane", "Azure Lane EX",
             ("rename", "Rename “Azure Lane” → “Azure Lane EX”", "stop", True)),
            ("Genshin", "azure lane",
             ("rename_clash", "Can't rename — “Azure Lane” already exists",
              "secondary", False)),
        ])

    def test_character_identity_rule_all_five_intents(self):
        review = self.review
        same, noun = review._name_same, "character alias"
        existing = ["Hu Tao", "Raiden"]
        self._check(same, noun, existing, [
            (None, "Nahida",
             ("new", "Save new character alias", "primary", True)),
            # spelling-only match (spacing) -> exists
            (None, "hutao",
             ("exists", "“Hu Tao” already exists — pick it above to edit",
              "secondary", False)),
            ("Hu Tao", "hu-tao",
             ("update", "Save changes", "primary", True)),
            ("Hu Tao", "Hutao 2",
             ("rename", "Rename “Hu Tao” → “Hutao 2”", "stop", True)),
            ("Raiden", "hutao",
             ("rename_clash", "Can't rename — “Hu Tao” already exists",
              "secondary", False)),
        ])

    def test_subreddit_identity_rule_all_five_intents(self):
        review = self.review
        same, noun = review._sub_same, "subreddit entry"
        existing = ["asuka", "rem"]
        self._check(same, noun, existing, [
            (None, "newsub",
             ("new", "Save new subreddit entry", "primary", True)),
            (None, "ASUKA",
             ("exists", "“asuka” already exists — pick it above to edit",
              "secondary", False)),
            ("asuka", "ASUKA",
             ("update", "Save changes", "primary", True)),
            ("asuka", "asuka2",
             ("rename", "Rename “asuka” → “asuka2”", "stop", True)),
            ("rem", "ASUKA",
             ("rename_clash", "Can't rename — “asuka” already exists",
              "secondary", False)),
        ])


# ---------------------------------------------------------------------------
# Part 2 -- the five panel handlers, driven against the synthetic tree.
#
# Each panel is described once (PANELS) so the two REGRESSION cases run over all
# five; the clash/exists/delete cases use one representative panel each.
# ---------------------------------------------------------------------------
PANELS = {
    "subreddit": dict(
        noun="subreddit entry", existing="renametest", newname="renamed2",
        setup=lambda r: r.on_settings_save("renametest", "Test Franchise", False, None, None),
        save=lambda r, key, sel, shown: r.on_settings_save(
            key, "Test Franchise", False, None, sel, shown),
        keys=lambda r: r._settings_keys(),
        present=lambda r, name: name in r._settings_raw(),
        same=lambda r: r._sub_same,
        file="subreddit_map.json",
        save_btn=lambda r: r.settings_save_btn,
        delete_btn=lambda r: r.settings_delete_btn,
        status=lambda r: r.settings_status_md,
        render=lambda r, sel: r._settings_render(selected=sel),
    ),
    "franchise": dict(
        noun="franchise alias", existing="Azure Lane", newname="Azure Lane EX",
        setup=lambda r: None,
        save=lambda r, key, sel, shown: r.on_fa_save(key, "Azur Lane", False, sel, shown),
        keys=lambda r: sorted(r._fa_table()),
        present=lambda r, name: name in r._fa_table(),
        same=lambda r: r.same_series_name,
        file="layout.json",
        save_btn=lambda r: r.fa_save_btn,
        delete_btn=lambda r: r.fa_delete_btn,
        status=lambda r: r.fa_status_md,
        render=lambda r, sel: r._fa_render(selected=sel),
    ),
    "character": dict(
        noun="character alias", existing="Raiden Shogun", newname="Ei",
        setup=lambda r: None,
        save=lambda r, key, sel, shown: r.on_ca_save(
            "Genshin Impact", key, "Raiden", sel, shown),
        keys=lambda r: sorted(r._ca_aliases("Genshin Impact")),
        present=lambda r, name: name in r._ca_aliases("Genshin Impact"),
        same=lambda r: r._name_same,
        file="layout.json",
        save_btn=lambda r: r.ca_save_btn,
        delete_btn=lambda r: r.ca_delete_btn,
        status=lambda r: r.ca_status_md,
        render=lambda r, sel: r._ca_render(folder="Genshin Impact", selected=sel),
    ),
    "series": dict(
        noun="series alias", existing="Re Zero", newname="Re Zero TV",
        setup=lambda r: None,
        save=lambda r, key, sel, shown: r.on_sa_save(key, "Re:Zero", sel, shown),
        keys=lambda r: sorted(r.load_series_aliases()),
        present=lambda r, name: name in r.load_series_aliases(),
        same=lambda r: r.same_series_name,
        file="data/series_aliases.json",
        save_btn=lambda r: r.sa_save_btn,
        delete_btn=lambda r: r.sa_delete_btn,
        status=lambda r: r.sa_status_md,
        render=lambda r, sel: r._sa_render(selected=sel),
    ),
    "shortname": dict(
        noun="shortname",
        existing="NIKKE The Goddess of Victory",
        newname="NIKKE The Goddess of Victory X",
        setup=lambda r: None,
        # a fresh code -- renaming the full name, code collision is a separate guard
        save=lambda r, key, sel, shown: r.on_sn_save("NKX", key, sel, shown),
        keys=lambda r: [f for _c, f in r._sn_entries()],
        present=lambda r, name: r._sn_find(name) is not None,
        same=lambda r: r.same_series_name,
        file="known_series_names.txt",
        save_btn=lambda r: r.sn_save_btn,
        delete_btn=lambda r: r.sn_delete_btn,
        status=lambda r: r.sn_status_md,
        render=lambda r, sel: r._sn_render(selected=sel),
    ),
}


class SettingsRenameGuardTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._tmp = tempfile.mkdtemp(prefix="oshiire-rename-")
        _build_panel_sandbox(Path(cls._tmp))
        # A private review copy bound to this sandbox (cwd, scrubbed env), whatever
        # order the test modules load in -- see tests/review_sandbox.py.
        cls._sandbox = ReviewSandbox(cls._tmp)
        cls.review = review = cls._sandbox.activate()

    @classmethod
    def tearDownClass(cls):
        cls._sandbox.deactivate()
        shutil.rmtree(cls._tmp, ignore_errors=True)

    def _reset_sandbox(self):
        """Rewrite the pristine stores and rebind review's in-memory globals.
        Safe to call repeatedly -- used both by setUp and between the sub-cases
        of the all-five-panels loops, which each mutate the tree."""
        review = self.review
        Path("layout.json").write_text(json.dumps(PANEL_LAYOUT, indent=2), encoding="utf-8")
        Path("data/series_aliases.json").write_text(json.dumps(SERIES_ALIASES, indent=2), encoding="utf-8")
        Path("known_series_names.txt").write_text(SHORTNAME_TEXT, encoding="utf-8")
        shutil.copy(REPO / "examples" / "subreddit_map.example.json", Path("subreddit_map.json"))

        review.layout.clear()
        review.layout.update(copy.deepcopy(PANEL_LAYOUT))
        review.series_aliases = review.load_series_aliases()
        review.shortname_entries = review.load_shortname_map(review.layout)

    def setUp(self):
        # review is a module singleton; snapshot its globals, then rebind to the
        # pristine stores, exactly like test_settings_config_editors.
        review = self.review
        self._layout_snap = copy.deepcopy(review.layout)
        self._series_snap = review.series_aliases
        self._shortname_snap = review.shortname_entries
        self._reset_sandbox()

    def tearDown(self):
        review = self.review
        review.layout.clear()
        review.layout.update(self._layout_snap)
        review.series_aliases = self._series_snap
        review.shortname_entries = self._shortname_snap

    def _call(self, fn, *args):
        with redirect_stdout(StringIO()):
            return fn(*args)

    def _rename_label(self, spec, selected, newname):
        review = self.review
        _intent, label, _v, _i = review._form_intent(
            selected, newname, spec["keys"](review), spec["same"](review), spec["noun"])
        return label

    # -- REGRESSION: a stale/absent button label never renames ---------------

    def test_rename_with_no_label_never_writes(self):
        """pick an entry, type a different name, Save with shown_label=None ->
        file bytes unchanged, old entry present, new absent. All five panels."""
        review = self.review
        for name, spec in PANELS.items():
            with self.subTest(panel=name):
                self._reset_sandbox()  # fresh tree per sub-case
                self._call(spec["setup"], review)
                before = Path(spec["file"]).read_bytes()
                self._call(spec["save"], review, spec["newname"],
                           spec["existing"], None)
                self.assertEqual(Path(spec["file"]).read_bytes(), before,
                                 f"{name}: the file must be byte-for-byte unchanged")
                self.assertTrue(spec["present"](review, spec["existing"]),
                                f"{name}: the picked entry must survive")
                self.assertFalse(spec["present"](review, spec["newname"]),
                                 f"{name}: the new name must NOT have been written")

    def test_rename_with_matching_label_renames(self):
        """the same, but Save with shown_label = the rename label the button
        shows -> renamed (old gone, new present, value carried over)."""
        review = self.review
        for name, spec in PANELS.items():
            with self.subTest(panel=name):
                self._reset_sandbox()
                self._call(spec["setup"], review)
                label = self._rename_label(spec, spec["existing"], spec["newname"])
                self._call(spec["save"], review, spec["newname"],
                           spec["existing"], label)
                self.assertFalse(spec["present"](review, spec["existing"]),
                                 f"{name}: the old name must be gone after a rename")
                self.assertTrue(spec["present"](review, spec["newname"]),
                                f"{name}: the new name must be present after a rename")

    def test_rename_carries_the_value_over(self):
        """franchise/series renames keep the mapping's value."""
        review = self.review
        label = self._rename_label(PANELS["franchise"], "Azure Lane", "Azure Lane EX")
        self._call(review.on_fa_save, "Azure Lane EX", "Azur Lane", False,
                   "Azure Lane", label)
        self.assertEqual(review._fa_table()["Azure Lane EX"], "Azur Lane")

        label = self._rename_label(PANELS["series"], "Re Zero", "Re Zero TV")
        self._call(review.on_sa_save, "Re Zero TV", "Re:Zero", "Re Zero", label)
        self.assertEqual(review.load_series_aliases()["Re Zero TV"], "Re:Zero")

    # -- rename_clash: renaming A onto an existing B refuses, names B --------

    def test_rename_clash_leaves_both_entries_and_names_the_clash(self):
        review = self.review
        # a second franchise alias to rename FROM
        self._call(review.on_fa_save, "Blue Archive", "Blue Archive", False, None)
        before = Path("layout.json").read_bytes()
        render = self._call(review.on_fa_save, "Azure Lane", "Azur Lane", False,
                            "Blue Archive", None)
        self.assertEqual(Path("layout.json").read_bytes(), before,
                         "a rename-clash must write nothing")
        table = review._fa_table()
        self.assertEqual(table["Azure Lane"], "Azur Lane")
        self.assertEqual(table["Blue Archive"], "Blue Archive")
        self.assertIn("Azure Lane", render[review.fa_status_md],
                      "the status must name the clashing entry")

    # -- exists: a respelling of an existing key, in new mode, writes no 2nd --

    def test_exists_respelling_writes_no_second_key(self):
        review = self.review
        render = self._call(review.on_fa_save, "azure lane", "Azur Lane", False, None)
        keys = list(review._fa_table())
        self.assertEqual(keys, ["Azure Lane"],
                         "a respelling in new mode must not add a second key")
        self.assertIn("already exists", render[review.fa_status_md])

    # -- Delete button label/interactivity straight from _render -------------

    def test_delete_button_tracks_the_selection(self):
        review = self.review
        for name, spec in PANELS.items():
            with self.subTest(panel=name):
                self._reset_sandbox()
                self._call(spec["setup"], review)  # subreddit: create the entry
                none_render = spec["render"](review, None)
                none_delete = none_render[spec["delete_btn"](review)]
                self.assertEqual(none_delete["value"], "Delete")
                self.assertFalse(none_delete["interactive"])

                sel_render = spec["render"](review, spec["existing"])
                sel_delete = sel_render[spec["delete_btn"](review)]
                self.assertEqual(sel_delete["value"], f"Delete “{spec['existing']}”")
                self.assertTrue(sel_delete["interactive"])

    def test_save_button_label_from_render(self):
        review = self.review
        for name, spec in PANELS.items():
            with self.subTest(panel=name):
                self._reset_sandbox()
                self._call(spec["setup"], review)
                # a picked entry, unedited -> "Save changes"
                picked = spec["render"](review, spec["existing"])
                self.assertEqual(picked[spec["save_btn"](review)]["value"], "Save changes")
                # a cleared form -> "Save new {noun}"
                fresh = spec["render"](review, None)
                self.assertEqual(fresh[spec["save_btn"](review)]["value"],
                                 f"Save new {spec['noun']}")

    # -- subreddit picker: an unknown typed value is NOT a pick --------------

    def test_subreddit_unknown_typed_value_reads_as_new(self):
        """The subreddit picker allows custom values; a typed-unknown string
        handed back must render 'Save new subreddit entry' with Delete off, not
        a spurious 'Save changes' / rename of a row that doesn't exist."""
        review = self.review
        self.assertNotIn("notasub", review._settings_raw())
        render = self._call(review._settings_render, "notasub")
        self.assertEqual(render[review.settings_save_btn]["value"],
                         "Save new subreddit entry")
        delete = render[review.settings_delete_btn]
        self.assertEqual(delete["value"], "Delete")
        self.assertFalse(delete["interactive"])
        # and the unknown value does not linger in the picker
        self.assertIsNone(render[review.settings_dropdown]["value"])

    def test_subreddit_saving_an_unknown_typed_value_adds_it(self):
        review = self.review
        before = len(review._settings_keys())
        # picker and box both hold the typed-unknown value, as the live UI does
        self._call(review.on_settings_save, "notasub", "Some Franchise", False,
                   None, "notasub", None)
        raw = review._settings_raw()
        self.assertIn("notasub", raw)
        self.assertEqual(raw["notasub"]["franchise"], "Some Franchise")
        self.assertEqual(len(review._settings_keys()), before + 1)

    def test_subreddit_rename_label_is_lowercased(self):
        """The label (and so the written name) is the normalised key, not the
        raw box text -- the render, the .input handler and the save handler all
        compute it the same way, so shown_label still matches."""
        review = self.review
        self._call(review.on_settings_save, "renametest", "Test Franchise",
                   False, None, None)
        # the live .input handler, as the user types a mixed-case new name
        save, _delete = self._call(review.on_settings_key_input, "RenamedSub", "renametest")
        self.assertEqual(save["value"], "Rename “renametest” → “renamedsub”")
        # and the save handler refuses with no label, renames with that label
        before = Path("subreddit_map.json").read_bytes()
        self._call(review.on_settings_save, "RenamedSub", "Test Franchise", False,
                   None, "renametest", None)
        self.assertEqual(Path("subreddit_map.json").read_bytes(), before,
                         "no label -> no rename")
        self._call(review.on_settings_save, "RenamedSub", "Test Franchise", False,
                   None, "renametest", "Rename “renametest” → “renamedsub”")
        raw = review._settings_raw()
        self.assertNotIn("renametest", raw)
        self.assertIn("renamedsub", raw, "renamed to the lowercased key")


if __name__ == "__main__":
    unittest.main(verbosity=2)
