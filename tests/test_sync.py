"""Tests for sync.py -- reconciling the manifest and the pHash index with an
archive the user has reorganised by hand.

The scenario that motivated the module is the first test here, taken from the
real archive: several Fatui characters were promoted out of the shared
`Genshin Impact/Fatui/` folder into their own (`Sandrone/`, `Arlecchino/`) in
Explorer. Nothing told oshiire, so seven manifest entries and seven index rows
went on naming a path that no longer existed -- and a row keyed on a vanished
path is compared against NOTHING, which is how duplicate detection silently
stops working.

What these pin down, in the order the bugs would bite:

  1. IDENTITY IS THE BASENAME, and nothing else. A gallery entry
     (t3_x_1.jpg / t3_x_2.jpg), a shortname suffix (t3_x_BtR.jpg) and a
     collision suffix (t3_x_2.jpg) are indistinguishable by any parse of the
     filename, so the tests put all three in one archive alongside a plain
     t3_x.jpg decoy. Anything that strips a trailing "_N" to recover a
     post-id will conflate them, and the conflation is silent: it rewrites an
     archive_path to point at a different image.
  2. Only the unambiguous bucket is applied. A file in a folder layout.json
     knows is a deliberate reorganisation; a file in a folder nobody has
     heard of, one basename in two folders, or a file that is simply gone are
     each ambiguous, and --apply must report them rather than guess.
  3. Dry-run writes NOTHING -- asserted on the manifest's bytes AND its
     mtime_ns, and on the index file, since a rewrite producing identical
     bytes still churns mtime (and, on a Drive-synced folder, an upload).
  4. A second run after --apply is entirely UNCHANGED and writes nothing.
     Reconcilers that are not idempotent get run once and then distrusted.
  5. A wallpaper copy is not a duplicate. archive.py copies a wallpaper under
     the SAME filename into Wallpaper/<PC|Telefon>/, so without the wallpaper
     exclusion every entry with a wallpaper copy would report DUPLICATED
     forever -- a false positive on entries that are perfectly in sync.
  6. The layout audit catches what routing cannot. A `character_aliases` key
     equal to the franchise's own name resolves cleanly, so archive.py never
     flags it; it just files every image tagged only "Genshin Impact" into
     Ayaka's folder. That live instance is the fixture.

Everything runs on a synthetic archive, manifest and index in a temp dir.
The files are one byte each and are never decoded -- sync.py reads names, not
pixels. Neither the real ARCHIVE_DIR, manifest.json, layout.json nor the real
index is read or written.

    python -m unittest discover -s tests
    python tests/test_sync.py
"""
import copy
import io
import shutil
import sys
import tempfile
import time
import unittest
from contextlib import redirect_stdout
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

import sync  # noqa: E402
from hash_index import (  # noqa: E402
    DEFAULT_HASH_SIZE,
    get_indexed_rel_paths,
    open_index,
    record_indexed_file,
)
from manifest import load_manifest, save_manifest  # noqa: E402

# Shaped like the real layout.json, with this user's actual special-folder
# names (the phone folder is "Telefon", not "Phone") so the special-dir
# enumeration is exercised as configured rather than as defaulted.
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
    "franchise_aliases": {},
    "character_aliases": {},
    "franchises": {
        "Genshin Impact": {
            "style": "nested",
            # Fatui is the shared catch-all folder the promotions came out of;
            # Sandrone and Arlecchino are the folders they went into.
            "characters": ["Fatui", "Sandrone", "Arlecchino", "Signora", "Ayaka"],
        },
        "Neon Ward": {"style": "flat"},
    },
}

FATUI = "Genshin Impact/Fatui"
SANDRONE = "Genshin Impact/Sandrone"
ARLECCHINO = "Genshin Impact/Arlecchino"


class SyncTestCase(unittest.TestCase):
    """A throwaway archive + manifest + index, assembled per test."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="oshiire-sync-"))
        self.archive = self.tmp / "archive"
        self.archive.mkdir()
        self.manifest_path = self.tmp / "manifest.json"
        self.db = self.tmp / "index.db"
        self.layout = copy.deepcopy(LAYOUT)
        self.manifest = {}

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    # -- fixture builders ---------------------------------------------------
    def touch(self, rel: str) -> Path:
        """Create a one-byte file at a POSIX rel path under the archive."""
        path = self.archive / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"x")
        return path

    def entry(self, key: str, archive_path: str, **extra) -> dict:
        entry = {
            "post_id": extra.pop("post_id", key),
            "title": f"art for {key}",
            "subreddit": "Genshin_Impact",
            "status": "archived",
            "archive_path": archive_path,
        }
        entry.update(extra)
        self.manifest[key] = entry
        return entry

    def save(self) -> None:
        save_manifest(self.manifest, self.manifest_path)

    def seed_index(self, *rel_paths: str) -> None:
        open_index(self.db, DEFAULT_HASH_SIZE).close()
        for i, rel in enumerate(rel_paths):
            # A distinct 64-bit hash per row; never compared here, only carried.
            phash = f"{i:016x}"
            record_indexed_file(self.db, rel, phash, 100 + i, 1700000000.0 + i, 800, 1200)

    # -- helpers ------------------------------------------------------------
    def plan(self, scope=None):
        return sync.build_plan(self.manifest, self.layout, self.archive, scope=scope)

    def buckets(self, plan) -> dict:
        return {item.key: item.bucket for item in plan.items}

    def destinations(self, plan) -> dict:
        return {item.key: item.new_rel for item in plan.items}

    def apply(self, plan):
        return sync.apply_plan(plan, self.manifest, self.db, self.manifest_path)

    def files_on_disk(self) -> set:
        return {
            p.relative_to(self.archive).as_posix()
            for p in self.archive.rglob("*") if p.is_file()
        }


class PromotedCharactersTest(SyncTestCase):
    """The live scenario: six files promoted out of Fatui, one left alone."""

    def setUp(self):
        super().setUp()
        self.promotions = {
            "t3_s1": SANDRONE, "t3_s2": SANDRONE, "t3_s3": SANDRONE,
            "t3_s4": SANDRONE, "t3_s5": SANDRONE, "t3_a1": ARLECCHINO,
        }
        seeded = []
        for key, new_folder in self.promotions.items():
            # Recorded in Fatui; physically already in the new folder.
            self.entry(key, f"{FATUI}/{key}.jpg")
            self.touch(f"{new_folder}/{key}.jpg")
            seeded.append(f"{FATUI}/{key}.jpg")

        # Signora was never promoted: recorded in Fatui, still in Fatui.
        self.entry("t3_sig", f"{FATUI}/t3_sig.jpg")
        self.touch(f"{FATUI}/t3_sig.jpg")
        seeded.append(f"{FATUI}/t3_sig.jpg")

        self.save()
        self.seed_index(*seeded)

    def test_six_repairable_moves_and_one_unchanged(self):
        plan = self.plan()
        buckets = self.buckets(plan)

        self.assertEqual(len(plan.by_bucket(sync.MOVED_OK)), 6)
        self.assertEqual(buckets["t3_sig"], sync.UNCHANGED)
        self.assertEqual(
            {key for key, bucket in buckets.items() if bucket == sync.MOVED_OK},
            set(self.promotions),
        )
        # Every repairable move must name the folder the file is actually in.
        destinations = self.destinations(plan)
        for key, folder in self.promotions.items():
            self.assertEqual(destinations[key], f"{folder}/{key}.jpg")

    def test_apply_rewrites_archive_paths_and_rekeys_the_index(self):
        result = self.apply(self.plan())

        self.assertEqual(result.moved, 6)
        self.assertEqual(result.index_updated, 6)
        self.assertEqual(result.index_missed, 0)
        self.assertTrue(result.manifest_written)

        on_disk = load_manifest(self.manifest_path)
        for key, folder in self.promotions.items():
            self.assertEqual(on_disk[key]["archive_path"], f"{folder}/{key}.jpg")
        # Signora's entry is untouched -- it was never in the repairable set.
        self.assertEqual(on_disk["t3_sig"]["archive_path"], f"{FATUI}/t3_sig.jpg")

        expected_index = {f"{folder}/{key}.jpg" for key, folder in self.promotions.items()}
        expected_index.add(f"{FATUI}/t3_sig.jpg")
        self.assertEqual(get_indexed_rel_paths(self.db), expected_index)

    def test_apply_never_touches_a_single_file(self):
        before = self.files_on_disk()

        self.apply(self.plan())

        self.assertEqual(self.files_on_disk(), before)

    def test_second_run_is_entirely_unchanged_and_writes_nothing(self):
        self.apply(self.plan())

        second = self.plan()
        self.assertEqual(
            set(self.buckets(second).values()), {sync.UNCHANGED},
            "a reconciler that is not idempotent gets run once and then distrusted",
        )

        manifest_bytes = self.manifest_path.read_bytes()
        manifest_mtime = self.manifest_path.stat().st_mtime_ns
        index_bytes = self.db.read_bytes()
        time.sleep(0.01)

        result = self.apply(second)

        self.assertEqual(result.moved, 0)
        self.assertFalse(result.manifest_written)
        self.assertEqual(self.manifest_path.read_bytes(), manifest_bytes)
        self.assertEqual(self.manifest_path.stat().st_mtime_ns, manifest_mtime)
        self.assertEqual(self.db.read_bytes(), index_bytes)

    def test_dry_run_writes_nothing(self):
        manifest_bytes = self.manifest_path.read_bytes()
        manifest_mtime = self.manifest_path.stat().st_mtime_ns
        index_bytes = self.db.read_bytes()
        index_mtime = self.db.stat().st_mtime_ns
        time.sleep(0.01)

        plan = self.plan()
        with redirect_stdout(io.StringIO()) as out:
            sync.print_report(plan, self.archive, self.db)

        self.assertEqual(self.manifest_path.read_bytes(), manifest_bytes)
        self.assertEqual(self.manifest_path.stat().st_mtime_ns, manifest_mtime)
        self.assertEqual(self.db.read_bytes(), index_bytes)
        self.assertEqual(self.db.stat().st_mtime_ns, index_mtime)
        # And the report actually said something about the six moves.
        self.assertIn("Repairable moves (6)", out.getvalue())

    def test_scope_limits_the_report(self):
        scoped = self.plan(scope=ARLECCHINO)

        self.assertEqual(
            {item.key for item in scoped.items}, {"t3_a1"},
            "scope filters on either end of a move, so Arlecchino must match the destination",
        )
        self.assertEqual(scoped.scope, ARLECCHINO)


class FilenameIdentityTest(SyncTestCase):
    """Gallery indices, shortname codes and collision suffixes all look like
    "_N" on the end of a filename. None of them may be parsed."""

    def test_gallery_siblings_match_their_own_entries(self):
        # One post, two images, two entries -- sharing a post_id, differing
        # only in the suffix that IS the identity here.
        self.entry("t3_g_1", f"{FATUI}/t3_g_1.jpg", post_id="t3_g", image_index=1)
        self.entry("t3_g_2", f"{FATUI}/t3_g_2.jpg", post_id="t3_g", image_index=2)
        self.touch(f"{SANDRONE}/t3_g_1.jpg")   # image 1 was promoted
        self.touch(f"{FATUI}/t3_g_2.jpg")      # image 2 was not
        self.save()

        plan = self.plan()

        self.assertEqual(
            self.buckets(plan), {"t3_g_1": sync.MOVED_OK, "t3_g_2": sync.UNCHANGED}
        )
        self.assertEqual(self.destinations(plan)["t3_g_1"], f"{SANDRONE}/t3_g_1.jpg")

    def test_shortname_and_collision_suffixes_are_not_parsed(self):
        # t3_x_BtR.jpg (shortname code) and t3_x_2.jpg (collision suffix) both
        # belong to entries of their own, and t3_x.jpg is a decoy that any
        # suffix-stripping match would grab instead.
        self.entry("t3_short", f"{FATUI}/t3_x_BtR.jpg")
        self.entry("t3_collided", f"{FATUI}/t3_x_2.jpg")
        self.touch(f"{SANDRONE}/t3_x_BtR.jpg")
        self.touch(f"{ARLECCHINO}/t3_x_2.jpg")
        self.touch(f"{FATUI}/t3_x.jpg")  # untracked decoy, belongs to no entry
        self.save()

        plan = self.plan()
        destinations = self.destinations(plan)

        self.assertEqual(
            self.buckets(plan),
            {"t3_short": sync.MOVED_OK, "t3_collided": sync.MOVED_OK},
        )
        self.assertEqual(destinations["t3_short"], f"{SANDRONE}/t3_x_BtR.jpg")
        self.assertEqual(destinations["t3_collided"], f"{ARLECCHINO}/t3_x_2.jpg")
        # The decoy is nobody's file and must simply be left alone.
        self.assertEqual(plan.untracked, [f"{FATUI}/t3_x.jpg"])


class AttentionBucketTest(SyncTestCase):
    """Everything --apply refuses to decide."""

    def test_vanished_when_no_file_carries_the_name(self):
        self.entry("t3_gone", f"{FATUI}/t3_gone.jpg")
        self.touch(f"{FATUI}/t3_other.jpg")
        self.entry("t3_other", f"{FATUI}/t3_other.jpg")
        self.save()

        plan = self.plan()

        self.assertEqual(self.buckets(plan)["t3_gone"], sync.VANISHED)
        self.assertEqual(plan.by_bucket(sync.MOVED_OK), [])

    def test_duplicated_when_one_basename_sits_in_two_folders(self):
        self.entry("t3_dup", f"{FATUI}/t3_dup.jpg")
        self.touch(f"{SANDRONE}/t3_dup.jpg")
        self.touch(f"{ARLECCHINO}/t3_dup.jpg")
        self.save()

        plan = self.plan()
        item = plan.items[0]

        self.assertEqual(item.bucket, sync.DUPLICATED)
        self.assertIsNone(item.new_rel, "a duplicated entry has no single destination to apply")
        self.assertIn(f"{SANDRONE}/t3_dup.jpg", item.detail)
        self.assertIn(f"{ARLECCHINO}/t3_dup.jpg", item.detail)
        self.assertEqual(self.apply(plan).moved, 0)

    def test_moved_unknown_is_reported_and_never_applied(self):
        self.entry("t3_unk", f"{FATUI}/t3_unk.jpg")
        self.touch("Genshin Impact/Sandrne/t3_unk.jpg")  # typo'd folder
        self.save()

        plan = self.plan()
        item = plan.items[0]

        self.assertEqual(item.bucket, sync.MOVED_UNKNOWN)
        self.assertEqual(item.new_rel, "Genshin Impact/Sandrne/t3_unk.jpg")

        result = self.apply(plan)

        self.assertEqual(result.moved, 0)
        self.assertFalse(result.manifest_written)
        self.assertEqual(self.manifest["t3_unk"]["archive_path"], f"{FATUI}/t3_unk.jpg")

    def test_manifest_collision_is_flagged_as_a_data_bug(self):
        self.entry("t3_one", f"{FATUI}/shared.jpg")
        self.entry("t3_two", f"{SANDRONE}/shared.jpg")
        self.touch(f"{FATUI}/shared.jpg")
        self.save()

        plan = self.plan()
        buckets = self.buckets(plan)

        self.assertEqual(
            buckets, {"t3_one": sync.MANIFEST_COLLISION, "t3_two": sync.MANIFEST_COLLISION}
        )
        self.assertIn("t3_two", plan.items[0].detail)
        self.assertEqual(self.apply(plan).moved, 0)

    def test_untracked_legacy_files_are_counted_and_left_alone(self):
        self.entry("t3_mine", f"{FATUI}/t3_mine.jpg")
        self.touch(f"{FATUI}/t3_mine.jpg")
        self.touch("Neon Ward/legacy-scan-001.jpg")
        self.touch("Neon Ward/pixiv_884412.png")
        self.save()

        plan = self.plan()

        self.assertEqual(self.buckets(plan), {"t3_mine": sync.UNCHANGED})
        self.assertEqual(
            plan.untracked,
            ["Neon Ward/legacy-scan-001.jpg", "Neon Ward/pixiv_884412.png"],
        )
        self.assertEqual(plan.disk_files, 3)


class WallpaperCopyTest(SyncTestCase):
    """archive.py copies a wallpaper under the SAME filename, so the basename
    legitimately exists twice. That must not read as a duplicate."""

    def test_wallpaper_copy_does_not_make_its_entry_duplicated(self):
        self.entry(
            "t3_wp", f"{FATUI}/t3_wp.jpg",
            wallpaper="pc", wallpaper_paths=["Wallpaper/PC/t3_wp.jpg"],
        )
        self.touch(f"{SANDRONE}/t3_wp.jpg")      # the primary was promoted
        self.touch("Wallpaper/PC/t3_wp.jpg")     # its wallpaper copy stayed put
        self.save()

        plan = self.plan()

        self.assertEqual(self.buckets(plan), {"t3_wp": sync.MOVED_OK})
        self.assertEqual(self.destinations(plan)["t3_wp"], f"{SANDRONE}/t3_wp.jpg")
        # And the copy is oshiire's own file, so it is not "untracked" either.
        self.assertEqual(plan.untracked, [])


class DestDirIsKnownTest(SyncTestCase):
    def test_roster_folder_is_known(self):
        self.assertTrue(sync.dest_dir_is_known(SANDRONE, self.layout))

    def test_roster_match_ignores_spacing_and_case(self):
        # Mirrors _lookup_character, so a folder routing would accept is never
        # called unknown here.
        self.assertTrue(sync.dest_dir_is_known("genshin impact/arlecchino", self.layout))

    def test_group_subfolder_is_known(self):
        self.assertTrue(sync.dest_dir_is_known("Genshin Impact/Others_Group", self.layout))

    def test_special_folders_are_known(self):
        for special in ("Crossover", "Wallpaper", "Wallpaper/PC", "Wallpaper/Telefon",
                        "Others/Known Series", "Others/Unknown Sauce",
                        "Others/Artist's Original"):
            with self.subTest(special=special):
                self.assertTrue(sync.dest_dir_is_known(special, self.layout))

    def test_franchise_root_is_known_for_both_styles(self):
        self.assertTrue(sync.dest_dir_is_known("Genshin Impact", self.layout))
        self.assertTrue(sync.dest_dir_is_known("Neon Ward", self.layout))

    def test_typo_folder_is_unknown(self):
        self.assertFalse(sync.dest_dir_is_known("Genshin Impact/Sandrne", self.layout))

    def test_unknown_franchise_is_unknown(self):
        self.assertFalse(sync.dest_dir_is_known("Some Other Series/Ayaka", self.layout))

    def test_subfolder_of_a_flat_franchise_is_unknown(self):
        # A flat franchise's folder IS the leaf -- a character subfolder under
        # it is not something routing could have produced.
        self.assertFalse(sync.dest_dir_is_known("Neon Ward/Someone", self.layout))

    def test_archive_root_is_unknown(self):
        self.assertFalse(sync.dest_dir_is_known("", self.layout))


class LayoutAuditTest(SyncTestCase):
    def _kinds(self, layout):
        return {(f.franchise, f.kind, f.alias) for f in sync.audit_layout(layout)}

    def test_clean_layout_flags_nothing(self):
        self.layout["character_aliases"] = {
            "Genshin Impact": {"Raiden Shogun": "Signora", "Hutao": "Ayaka"},
        }

        self.assertEqual(sync.audit_layout(self.layout), [])

    def test_alias_key_equal_to_the_franchise_name(self):
        # The live instance: every image tagged only "Genshin Impact", with no
        # character at all, silently files into Ayaka's folder. Routing
        # resolves it cleanly, so nothing else in the tree can report it.
        self.layout["character_aliases"] = {"Genshin Impact": {"Genshin Impact": "Ayaka"}}

        findings = sync.audit_layout(self.layout)

        self.assertEqual(len(findings), 1)
        self.assertEqual(findings[0].kind, sync.ALIAS_KEY_IS_FRANCHISE)
        self.assertEqual(findings[0].franchise, "Genshin Impact")
        self.assertEqual(findings[0].target, "Ayaka")

    def test_alias_value_that_is_not_a_roster_folder(self):
        self.layout["character_aliases"] = {"Genshin Impact": {"Raiden Shogun": "Raiden Ei"}}

        findings = sync.audit_layout(self.layout)

        self.assertEqual([f.kind for f in findings], [sync.ALIAS_VALUE_NOT_A_FOLDER])
        self.assertEqual(findings[0].alias, "Raiden Shogun")

    def test_alias_key_that_shadows_a_roster_folder(self):
        self.layout["character_aliases"] = {"Genshin Impact": {"Ayaka": "Sandrone"}}

        findings = sync.audit_layout(self.layout)

        self.assertEqual([f.kind for f in findings], [sync.ALIAS_KEY_SHADOWS_ROSTER])
        self.assertEqual(findings[0].alias, "Ayaka")

    def test_an_alias_that_only_respells_its_own_folder_is_not_shadowing(self):
        # Both of these exist in the real layout.json ("WIz" -> "Wiz", a stray
        # apostrophe on a Konosuba name). The alias key resolves to the roster
        # folder its value already names, so it changes nothing -- reporting it
        # would bury the real findings under typo-shaped noise, and an audit
        # whose output is mostly noise stops being read.
        self.layout["character_aliases"] = {
            "Genshin Impact": {"AYAKA": "Ayaka", "Sandrone'": "Sandrone"},
        }

        self.assertEqual(sync.audit_layout(self.layout), [])

    def test_all_three_are_reported_together(self):
        self.layout["character_aliases"] = {
            "Genshin Impact": {
                "Genshin Impact": "Ayaka",
                "Raiden Shogun": "Raiden Ei",
                "Signora": "Sandrone",
            },
        }

        self.assertEqual(
            self._kinds(self.layout),
            {
                ("Genshin Impact", sync.ALIAS_KEY_IS_FRANCHISE, "Genshin Impact"),
                ("Genshin Impact", sync.ALIAS_VALUE_NOT_A_FOLDER, "Raiden Shogun"),
                ("Genshin Impact", sync.ALIAS_KEY_SHADOWS_ROSTER, "Signora"),
            },
        )

    def test_a_franchise_with_no_roster_only_checks_the_franchise_name_key(self):
        # A flat franchise has no subfolders, so "value is not a folder" and
        # "key shadows a folder" have nothing to compare against and would
        # otherwise fire on every alias while saying nothing.
        self.layout["character_aliases"] = {
            "Neon Ward": {"Neon Ward": "Somebody", "Someone Else": "Whoever"},
        }

        findings = sync.audit_layout(self.layout)

        self.assertEqual([f.kind for f in findings], [sync.ALIAS_KEY_IS_FRANCHISE])

    def test_audit_is_independent_of_the_reconcile_plan(self):
        self.layout["character_aliases"] = {"Genshin Impact": {"Genshin Impact": "Ayaka"}}
        self.entry("t3_mine", f"{FATUI}/t3_mine.jpg")
        self.touch(f"{FATUI}/t3_mine.jpg")
        self.save()

        plan = self.plan()

        self.assertEqual(self.buckets(plan), {"t3_mine": sync.UNCHANGED})
        self.assertEqual([f.kind for f in plan.audit], [sync.ALIAS_KEY_IS_FRANCHISE])


if __name__ == "__main__":
    unittest.main(verbosity=2)
