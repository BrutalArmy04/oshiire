"""Tests for review.py's two engine-facing Settings tabs: Character Folders
(promote / merge) and Sync (scan / reconcile / audit).

Both tabs are a FACE on engines that are already tested elsewhere
(tests/test_character_aliases.py for promote_character/merge_character,
tests/test_sync.py for the reconcile plan). What is untested until here is the
wiring, and the wiring is where the damage would be:

  1. The buttons must call the ENGINE, not reimplement it. A tab that edited
     layout.json directly would pass an "is the alias gone?" assertion while
     skipping the roster edit and the group-route clear that promote_character
     does in the same atomic write -- leaving a half-applied promotion, which
     is a routing bug that shows up an image at a time, weeks later.
  2. The merge dropdown must exclude the row's own name. merge_character
     raises ValueError on a self-merge because it would drop the very roster
     entry the new alias points at; the ONLY thing standing between a user and
     that exception is the dropdown's contents.
  3. The post-edit counter is the tab's whole safety story. layout.json decides
     where FUTURE images go; the ones already archived do not move, and the
     count is what tells the user how many they still have to drag across. A
     wrong count reads as "nothing to do".
  4. Scan must write NOTHING. It is the button a nervous user presses to look,
     and asserted here on the manifest's bytes AND its mtime_ns -- a rewrite
     producing identical bytes still churns mtime and, on a Drive-synced
     folder, an upload.
  5. Apply is gated on a scan. A plan describes the archive at the moment it
     was built; applying without one, or after leaving the tab to go and move
     files, would rewrite archive_path to somewhere the file is not.

Everything runs in a temp cwd against a synthetic archive, manifest and
layout. The real manifest.json, layout.json and ARCHIVE_DIR are never read or
written -- ARCHIVE_DIR is pointed at the sandbox for the duration and put back
in tearDownClass.

    python -m unittest discover -s tests
    python tests/test_settings_tabs.py
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
from unittest.mock import patch

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

import shortname as shortname_module  # noqa: E402
import sync  # noqa: E402
from manifest import load_manifest, save_manifest  # noqa: E402

FOLDER = "Starfall Chronicle"
OTHER = "Lantern District"

# Shaped like the real layout.json. Four franchises on purpose: two the tab may
# edit, one flat and one nested-but-empty that it must filter OUT (a flat
# franchise never puts the character name in the path, and an empty roster has
# no folder to group anything into -- the one thing merge_character refuses).
LAYOUT = {
    "group_subfolder": "Others_Group",
    "special_folders": {
        "crossover": "Crossover",
        "wallpaper_root": "Wallpaper",
        "wallpaper_pc": "PC",
        "wallpaper_phone": "Telefon",
        "others_oc": "Others/Artist's Original",
        "others_unknown_source": "Others/Unknown Sauce",
        "others_known_series": "Others/Known Series",
    },
    "shortname_file": "known_series_names.txt",
    "franchise_aliases": {},
    "character_aliases": {
        # "Serah" is grouped in "Sera" -- the row the promote test moves.
        FOLDER: {"Serah": "Sera"},
        # An alias key equal to the franchise's own name: the audit finding
        # sync.py exists to surface, kept on the OTHER franchise so it cannot
        # perturb the Character Folders rows under test.
        OTHER: {OTHER: "Yuzu Hoshimi"},
    },
    "franchises": {
        FOLDER: {"style": "nested", "characters": ["Sera", "Kestrel", "Vela Quinn"]},
        OTHER: {"style": "nested", "characters": ["Yuzu Hoshimi"]},
        "Neon Ward": {"style": "flat"},
        "Empty Roster": {"style": "nested", "characters": []},
    },
}


def _build_sandbox(tmp: Path) -> None:
    """A minimal but REAL project tree: review.py reads all of this at import."""
    (tmp / "layout.json").write_text(json.dumps(LAYOUT, indent=2), encoding="utf-8")
    shutil.copy(REPO / "known_series_names.example.txt", tmp / "known_series_names.txt")
    # tagger.subreddit_is_mapped exits the process if this is absent.
    shutil.copy(REPO / "subreddit_map.example.json", tmp / "subreddit_map.json")
    (tmp / "staging").mkdir()
    (tmp / "manifest.json").write_text("{}", encoding="utf-8")
    (tmp / "archive").mkdir()


class SettingsTabsTestCase(unittest.TestCase):
    """Shared sandbox + one review import. review.py binds its manifest, layout
    and queue at IMPORT time from whatever cwd it was first imported in, so the
    module is really a handle on one sandbox -- hence the sys.modules dance,
    and hence every test below driving the module's globals rather than
    re-importing."""

    @classmethod
    def setUpClass(cls):
        cls._tmp = tempfile.mkdtemp(prefix="oshiire-tabs-")
        cls._prev_cwd = os.getcwd()
        tmp = Path(cls._tmp)
        _build_sandbox(tmp)
        os.chdir(tmp)
        # Pointed at the SANDBOX archive, not unset: the Sync tab is the one
        # panel that needs an ARCHIVE_DIR to do anything at all.
        cls._prev_archive = os.environ.pop("ARCHIVE_DIR", None)
        os.environ["ARCHIVE_DIR"] = str(tmp / "archive")
        # tests/test_render_contract.py imports review too, and leaving our
        # copy in sys.modules would hand that suite THIS sandbox's manifest.
        # Take the slot, then give it back.
        cls._prev_module = sys.modules.pop("review", None)
        with redirect_stdout(StringIO()):
            import review
        cls.review = review

    @classmethod
    def tearDownClass(cls):
        os.chdir(cls._prev_cwd)
        if cls._prev_archive is not None:
            os.environ["ARCHIVE_DIR"] = cls._prev_archive
        else:
            os.environ.pop("ARCHIVE_DIR", None)
        if cls._prev_module is not None:
            sys.modules["review"] = cls._prev_module
        else:
            sys.modules.pop("review", None)
        shutil.rmtree(cls._tmp, ignore_errors=True)

    def setUp(self):
        review = self.review
        self.tmp = Path(self._tmp)
        self.archive = self.tmp / "archive"
        self.manifest_path = self.tmp / "manifest.json"
        self.layout_path = self.tmp / "layout.json"

        # Rewind layout.json and review's in-memory layout to the fixture. The
        # dict is mutated IN PLACE, never rebound: promote_character and
        # merge_character take the object review.py already holds, and every
        # other panel in the app holds the same one.
        self.layout_fixture = copy.deepcopy(LAYOUT)
        self.layout_path.write_text(json.dumps(self.layout_fixture, indent=2), encoding="utf-8")
        review.layout.clear()
        review.layout.update(copy.deepcopy(self.layout_fixture))

        review.manifest.clear()
        review.sync_plan = None
        review.sync_db_path = self.tmp / "index.db"

        shutil.rmtree(self.archive, ignore_errors=True)
        self.archive.mkdir()

    # -- fixture builders ---------------------------------------------------
    def touch(self, rel: str) -> Path:
        path = self.archive / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"x")
        return path

    def archived(self, key: str, archive_path: str, characters=()) -> dict:
        entry = {
            "post_id": key,
            "title": f"art for {key}",
            "subreddit": "TestSub",
            "status": "archived",
            "archive_path": archive_path,
            "character_guess": list(characters),
            "franchise": [FOLDER],
        }
        self.review.manifest[key] = entry
        return entry

    def save(self):
        save_manifest(self.review.manifest, self.manifest_path)


# --------------------------------------------------------------------------- #
# Tab A -- Character Folders
# --------------------------------------------------------------------------- #
class CharacterFoldersTabTest(SettingsTabsTestCase):
    def own(self, franchise=FOLDER):
        return self.review._character_rows(franchise)[0]

    def grouped(self, franchise=FOLDER):
        return dict(self.review._character_rows(franchise)[1])

    # -- the dropdown only offers franchises the tab can actually edit -------

    def test_only_nested_franchises_with_a_roster_are_offered(self):
        self.assertEqual(self.review._nested_franchises(), [OTHER, FOLDER])

    # -- 1. the buttons call the ENGINE, and the row moves -------------------

    def test_promote_calls_the_engine_and_moves_the_row(self):
        self.assertEqual(self.grouped(), {"Serah": "Sera"})
        self.assertNotIn("Serah", self.own())

        real = self.review.promote_character
        with patch.object(self.review, "promote_character", wraps=real) as spy:
            self.review.on_character_promote(FOLDER, "Serah", "Sera")

        spy.assert_called_once()
        args = spy.call_args.args
        self.assertEqual(args[0], FOLDER)
        self.assertEqual(args[1], "Serah")
        self.assertIs(args[2], self.review.layout, "the engine must edit the LIVE layout")

        self.assertNotIn("Serah", self.grouped(), "still grouped after promotion")
        self.assertIn("Serah", self.own(), "promoted name has no folder of its own")
        # ...and the write reached disk, not just the in-memory dict.
        on_disk = json.loads(self.layout_path.read_text(encoding="utf-8"))
        self.assertIn("Serah", on_disk["franchises"][FOLDER]["characters"])
        self.assertNotIn("Serah", on_disk.get("character_aliases", {}).get(FOLDER, {}))

    def test_merge_calls_the_engine_and_moves_the_row_the_other_way(self):
        self.assertIn("Kestrel", self.own())
        self.assertNotIn("Kestrel", self.grouped())

        real = self.review.merge_character
        with patch.object(self.review, "merge_character", wraps=real) as spy:
            self.review.on_character_merge(FOLDER, "Kestrel", "Sera")

        spy.assert_called_once()
        args = spy.call_args.args
        self.assertEqual(args[:3], (FOLDER, "Kestrel", "Sera"))
        self.assertIs(args[3], self.review.layout)

        self.assertNotIn("Kestrel", self.own(), "merged name still has its own folder")
        self.assertEqual(self.grouped().get("Kestrel"), "Sera")
        on_disk = json.loads(self.layout_path.read_text(encoding="utf-8"))
        self.assertEqual(on_disk["character_aliases"][FOLDER]["Kestrel"], "Sera")
        self.assertNotIn("Kestrel", on_disk["franchises"][FOLDER]["characters"])

    def test_promote_and_merge_round_trip(self):
        """The two are inverses, so a round trip must land back on the fixture
        -- a promote that quietly left the alias behind would still pass the
        one-way assertions above."""
        self.review.on_character_merge(FOLDER, "Kestrel", "Sera")
        self.review.on_character_promote(FOLDER, "Kestrel", "Sera")

        self.assertIn("Kestrel", self.own())
        self.assertNotIn("Kestrel", self.grouped())

    # -- 2. the self-merge ValueError is unreachable from the UI -------------

    def test_merge_targets_exclude_the_rows_own_name(self):
        targets = self.review._merge_targets(FOLDER, "Sera")
        self.assertNotIn("Sera", targets)
        self.assertEqual(sorted(targets), ["Kestrel", "Vela Quinn"])

    def test_merge_targets_exclude_a_respelling_of_the_rows_own_name(self):
        """Exclusion is on normalize_name_key, the same rule merge_character
        raises on -- so "vela quinn" cannot sneak the row's own folder back into
        its own dropdown."""
        self.assertNotIn("Vela Quinn", self.review._merge_targets(FOLDER, "vela-quinn"))

    def test_the_excluded_call_is_the_one_that_would_raise(self):
        """Pins WHY the exclusion exists: the value the dropdown withholds is
        exactly the value the engine rejects."""
        with self.assertRaises(ValueError):
            shortname_module.merge_character(
                FOLDER, "Sera", "Sera", self.review.layout, self.layout_path
            )

    def test_merging_into_nothing_is_a_message_not_an_exception(self):
        result = self.review.on_character_merge(FOLDER, "Kestrel", "")
        self.assertIn("Pick a folder", result[self.review.char_status_md])
        self.assertIn("Kestrel", self.own(), "a no-op must not edit the layout")

    # -- 3. the "still need moving" counter ---------------------------------

    def test_counter_counts_only_matching_name_under_the_old_folder(self):
        # Three that count: archived, tagged Serah, still under Sera/.
        self.archived("t3_a1", f"{FOLDER}/Sera/t3_a1.jpg", ["Serah"])
        self.archived("t3_a2", f"{FOLDER}/Sera/t3_a2.jpg", ["Serah", "Kestrel"])
        # Spelling variants of the same name -- the manifest stores whatever
        # the post said, so matching is normalize_name_key, not equality.
        self.archived("t3_a3", f"{FOLDER}/Sera/t3_a3.jpg", ["serah"])
        # Right name, wrong folder: already moved, nothing left to do.
        self.archived("t3_b1", f"{FOLDER}/Serah/t3_b1.jpg", ["Serah"])
        # Right folder, different character: not this promotion's business.
        self.archived("t3_b2", f"{FOLDER}/Sera/t3_b2.jpg", ["Sera"])
        # Right name and folder, but never archived.
        self.review.manifest["t3_b3"] = {
            "post_id": "t3_b3", "status": "approved",
            "archive_path": f"{FOLDER}/Sera/t3_b3.jpg", "character_guess": ["Serah"],
        }

        self.assertEqual(self.review._archived_count_under("Serah", f"{FOLDER}/Sera"), 3)

    def test_counter_does_not_leak_across_sibling_folders(self):
        """`_under` is folder-boundary aware: "Sera" must not swallow "Seraph"."""
        self.archived("t3_c1", f"{FOLDER}/Seraph/t3_c1.jpg", ["Serah"])
        self.assertEqual(self.review._archived_count_under("Serah", f"{FOLDER}/Sera"), 0)

    def test_promote_status_line_names_both_folders_and_the_count(self):
        self.archived("t3_a1", f"{FOLDER}/Sera/t3_a1.jpg", ["Serah"])
        self.archived("t3_a2", f"{FOLDER}/Sera/t3_a2.jpg", ["Serah"])

        result = self.review.on_character_promote(FOLDER, "Serah", "Sera")
        status = result[self.review.char_status_md]

        self.assertIn("**2** archived image(s) for Serah", status)
        self.assertIn(f"{FOLDER}/Sera/", status)
        self.assertIn(f"{FOLDER}/Serah/", status)
        self.assertIn("Sync", status)

    def test_merge_status_line_points_at_the_folder_it_was_grouped_into(self):
        self.archived("t3_k1", f"{FOLDER}/Kestrel/t3_k1.jpg", ["Kestrel"])

        result = self.review.on_character_merge(FOLDER, "Kestrel", "Sera")
        status = result[self.review.char_status_md]

        self.assertIn("**1** archived image(s) for Kestrel", status)
        self.assertIn(f"{FOLDER}/Kestrel/", status)
        self.assertIn(f"{FOLDER}/Sera/", status)

    def test_nothing_to_move_says_so_rather_than_reporting_zero(self):
        result = self.review.on_character_promote(FOLDER, "Serah", "Sera")
        self.assertIn("No archived images", result[self.review.char_status_md])

    # -- the tab never touches a file --------------------------------------

    def test_no_edit_ever_touches_the_archive(self):
        self.touch(f"{FOLDER}/Sera/t3_a1.jpg")
        before = {p.relative_to(self.archive).as_posix()
                  for p in self.archive.rglob("*") if p.is_file()}

        self.review.on_character_promote(FOLDER, "Serah", "Sera")
        self.review.on_character_merge(FOLDER, "Kestrel", "Sera")

        after = {p.relative_to(self.archive).as_posix()
                 for p in self.archive.rglob("*") if p.is_file()}
        self.assertEqual(after, before)

    # -- the repaint contract ----------------------------------------------

    def test_every_edit_repaints_every_registered_component(self):
        """Same discipline as tests/test_render_contract.py: gradio fills an
        omitted component with skip(), so a partial repaint is silently
        accepted -- and a partial repaint is how a stale status line survives
        the edit that should have replaced it."""
        registered = {
            self.review.char_franchise_dropdown, self.review.char_count_md,
            self.review.char_status_md, self.review.char_tick,
        }
        for result in (
            self.review.on_character_promote(FOLDER, "Serah", "Sera"),
            self.review.on_character_merge(FOLDER, "Kestrel", "Sera"),
            self.review.on_character_select(FOLDER),
            self.review._character_tab_open(FOLDER),
        ):
            self.assertEqual(set(result), registered)

    def test_an_edit_advances_the_tick_and_a_selection_does_not(self):
        """The tick is what re-runs the dynamic row block. An edit must bump it
        (the rows it changed are the rows on screen); selecting a franchise must
        not, because the dropdown's own input event already re-renders them --
        and a second render would double every button's wiring."""
        before = self.review.on_character_select(FOLDER)[self.review.char_tick]
        after_open = self.review._character_tab_open(FOLDER)[self.review.char_tick]
        self.assertEqual(after_open, before)

        after_edit = self.review.on_character_promote(
            FOLDER, "Serah", "Sera")[self.review.char_tick]
        self.assertGreater(after_edit, before)


    # -- the dynamic row block actually builds ------------------------------

    def test_the_row_block_renders_and_wires_every_branch(self):
        """The per-row controls are built by a @gr.render function, which runs
        at REQUEST time -- nothing in the module body or in the helpers above
        would notice it raising. Every other test here drives the handlers
        directly and would pass over a tab that fails the moment it is opened.

        Rendered into review.demo because that is the Blocks the components and
        their .click wirings belong to; it is the last thing this class does to
        the module, and the process is thrown away after it. Three branches,
        because each builds a different set of components: no selection, a real
        franchise (rows + dropdowns + buttons), and a franchise that has since
        left the layout.
        """
        review = self.review
        with review.demo:
            for franchise in (None, FOLDER, "Not A Franchise Any More"):
                with self.subTest(franchise=franchise):
                    review._character_rows_ui(franchise, review.character_tick)


# --------------------------------------------------------------------------- #
# Tab B -- Sync
# --------------------------------------------------------------------------- #
class SyncTabTest(SettingsTabsTestCase):
    def seed(self):
        """One entry per bucket the report has to render, plus an untracked
        file. Recorded paths and disk paths deliberately disagree."""
        self.archived("t3_move", f"{FOLDER}/Sera/t3_move.jpg", ["Sera"])
        self.touch(f"{FOLDER}/Kestrel/t3_move.jpg")          # -> MOVED_OK

        self.archived("t3_same", f"{FOLDER}/Sera/t3_same.jpg", ["Sera"])
        self.touch(f"{FOLDER}/Sera/t3_same.jpg")             # -> UNCHANGED

        self.archived("t3_gone", f"{FOLDER}/Sera/t3_gone.jpg", ["Sera"])
                                                             # -> VANISHED

        self.archived("t3_odd", f"{FOLDER}/Sera/t3_odd.jpg", ["Sera"])
        self.touch("Scratch/t3_odd.jpg")                     # -> MOVED_UNKNOWN

        self.touch("Legacy/handfiled.jpg")                   # -> untracked
        self.save()

    def scan(self, scope=""):
        return self.review.on_sync_scan(scope)

    def report(self, result):
        return result[self.review.sync_report_md]

    def status(self, result):
        return result[self.review.sync_status_md]

    def apply_enabled(self, result):
        return result[self.review.sync_apply_btn]["interactive"]

    # -- 4. Scan classifies, and writes nothing -----------------------------

    def test_scan_renders_every_bucket(self):
        self.seed()
        result = self.scan()

        plan = self.review.sync_plan
        self.assertIsNotNone(plan, "Scan must leave a plan behind for Apply")
        buckets = {item.key: item.bucket for item in plan.items}
        self.assertEqual(buckets, {
            "t3_move": sync.MOVED_OK,
            "t3_same": sync.UNCHANGED,
            "t3_gone": sync.VANISHED,
            "t3_odd": sync.MOVED_UNKNOWN,
        })

        report = self.report(result)
        for bucket in sync.BUCKET_ORDER:
            self.assertIn(sync.BUCKET_LABELS[bucket], report)
        self.assertIn(sync.UNTRACKED_LABEL, report)
        # The repairable move is listed in full: old path AND new path.
        self.assertIn(f"{FOLDER}/Sera/t3_move.jpg", report)
        self.assertIn(f"{FOLDER}/Kestrel/t3_move.jpg", report)
        # Each attention item carries its detail.
        self.assertIn("no file with this name under ARCHIVE_DIR", report)
        self.assertIn("is not a folder layout.json can file to", report)
        # ...and the untracked count.
        self.assertIn("Untracked files (1)", report)

    def test_scan_renders_the_layout_audit(self):
        self.seed()
        report = self.report(self.scan())
        self.assertIn(sync.ALIAS_KEY_IS_FRANCHISE, report)
        self.assertIn("alias key is the franchise's own name", report)

    def test_scan_writes_nothing(self):
        """Asserted on bytes AND mtime_ns: a rewrite producing identical bytes
        still churns mtime and, on a Drive-synced folder, an upload."""
        self.seed()
        before_bytes = self.manifest_path.read_bytes()
        before_mtime = self.manifest_path.stat().st_mtime_ns
        layout_bytes = self.layout_path.read_bytes()
        files_before = {p.relative_to(self.archive).as_posix()
                        for p in self.archive.rglob("*") if p.is_file()}

        self.scan()

        self.assertEqual(self.manifest_path.read_bytes(), before_bytes)
        self.assertEqual(self.manifest_path.stat().st_mtime_ns, before_mtime)
        self.assertEqual(self.layout_path.read_bytes(), layout_bytes)
        self.assertFalse(self.review.sync_db_path.exists(),
                         "a dry run must not create the index")
        self.assertEqual(
            {p.relative_to(self.archive).as_posix()
             for p in self.archive.rglob("*") if p.is_file()},
            files_before,
        )
        self.assertIn("nothing was written", self.status(self.scan()))

    def test_scope_limits_the_report(self):
        self.seed()
        self.touch("Neon Ward/t3_flat.jpg")
        self.archived("t3_flat", "Neon Ward/t3_flat.jpg", [])
        self.save()

        self.scan(scope="Neon Ward")
        keys = {item.key for item in self.review.sync_plan.items}
        self.assertEqual(keys, {"t3_flat"})

    # -- 5. Apply is gated on a scan, and only then writes -------------------

    def test_apply_is_disabled_before_a_scan(self):
        self.seed()
        self.assertFalse(self.apply_enabled(self.review._sync_tab_open()))

    def test_apply_before_a_scan_writes_nothing_and_says_so(self):
        self.seed()
        before_mtime = self.manifest_path.stat().st_mtime_ns
        self.review.sync_plan = None

        result = self.review.on_sync_apply()

        self.assertIn("Run **Scan** first", self.status(result))
        self.assertFalse(self.apply_enabled(result))
        self.assertEqual(self.manifest_path.stat().st_mtime_ns, before_mtime)

    def test_a_scan_with_repairable_moves_enables_apply(self):
        self.seed()
        self.assertTrue(self.apply_enabled(self.scan()))

    def test_a_scan_with_nothing_repairable_leaves_apply_disabled(self):
        self.archived("t3_same", f"{FOLDER}/Sera/t3_same.jpg", ["Sera"])
        self.touch(f"{FOLDER}/Sera/t3_same.jpg")
        self.save()

        result = self.scan()
        self.assertFalse(self.apply_enabled(result))
        self.assertIn("nothing to repair", self.status(result))

    def test_apply_calls_the_engine_and_reports_what_changed(self):
        self.seed()
        self.scan()

        real = sync.apply_plan
        with patch.object(sync, "apply_plan", wraps=real) as spy:
            result = self.review.on_sync_apply()

        spy.assert_called_once()
        args = spy.call_args.args
        self.assertIs(args[1], self.review.manifest,
                      "apply must commit the LIVE manifest, not a second copy")

        # The repairable move, and only it, was recorded.
        self.assertEqual(self.review.manifest["t3_move"]["archive_path"],
                         f"{FOLDER}/Kestrel/t3_move.jpg")
        self.assertEqual(self.review.manifest["t3_odd"]["archive_path"],
                         f"{FOLDER}/Sera/t3_odd.jpg")
        self.assertEqual(load_manifest(self.manifest_path)["t3_move"]["archive_path"],
                         f"{FOLDER}/Kestrel/t3_move.jpg", "the write must reach disk")

        report = self.report(result)
        self.assertIn("1 archive_path(s) rewritten", report)
        self.assertIn(f"{FOLDER}/Sera/t3_move.jpg", report)
        self.assertIn(f"{FOLDER}/Kestrel/t3_move.jpg", report)
        self.assertIn("No file was moved, copied or deleted", self.status(result))

    def test_apply_hints_at_a_rebuild_when_the_index_missed_a_row(self):
        """The index is a rebuildable cache, but a file missing from it is
        compared against NOTHING -- so the hint is load-bearing, not decoration.
        Here the index does not exist at all, so every row misses."""
        self.seed()
        self.scan()
        report = self.report(self.review.on_sync_apply())
        self.assertIn("hash_index.py build", report)

    def test_apply_never_touches_a_file(self):
        self.seed()
        before = {p.relative_to(self.archive).as_posix()
                  for p in self.archive.rglob("*") if p.is_file()}
        self.scan()
        self.review.on_sync_apply()
        after = {p.relative_to(self.archive).as_posix()
                 for p in self.archive.rglob("*") if p.is_file()}
        self.assertEqual(after, before)

    def test_the_plan_is_spent_after_apply(self):
        """It described the archive BEFORE the write. Re-enabling Apply on it
        would let a second click rewrite paths off a stale reading."""
        self.seed()
        self.scan()
        result = self.review.on_sync_apply()

        self.assertIsNone(self.review.sync_plan)
        self.assertFalse(self.apply_enabled(result))
        self.assertIn("Run **Scan** first", self.status(self.review.on_sync_apply()))

    def test_opening_the_tab_discards_an_earlier_plan(self):
        """The user leaves this tab precisely to go and move files."""
        self.seed()
        self.scan()
        self.assertIsNotNone(self.review.sync_plan)

        result = self.review._sync_tab_open()

        self.assertIsNone(self.review.sync_plan)
        self.assertFalse(self.apply_enabled(result))
        self.assertIn("writes nothing", self.status(result))

    # -- the repaint contract ----------------------------------------------

    def test_every_sync_event_repaints_every_registered_component(self):
        self.seed()
        registered = {
            self.review.sync_report_md, self.review.sync_status_md,
            self.review.sync_apply_btn,
        }
        self.assertEqual(set(self.review._sync_tab_open()), registered)
        self.assertEqual(set(self.scan()), registered)
        self.assertEqual(set(self.review.on_sync_apply()), registered)


if __name__ == "__main__":
    unittest.main()
