"""Regression test: the committed example configs must keep routing correctly.

Loads `manifest.example.json` and `layout.example.json` through their REAL
consumers (`manifest.py`, `shortname.py`), runs `archive.py`'s actual routing
engine over them, and asserts the resulting plan matches
`tests/expected_dry_run.txt` byte for byte.

Why this exists: the example configs and the router can drift apart silently.
A routing-precedence change, a renamed special folder, or a gutted example
entry all leave both files individually valid while the documentation they form
together quietly stops being true. Nothing else in the tree notices.

The test builds a throwaway archive in a temp dir -- `plan_moves` flags a
destination that does not exist, and refuses to plan an entry whose staging file
is missing, so both have to be stood up. Nothing under the repo, and nothing
under the real ARCHIVE_DIR, is read or written.

Run it either way -- there is no test dependency to install:
    python -m unittest discover -s tests        # stdlib
    python tests/test_example_configs.py        # direct
    pytest tests/                               # if you have it
"""
import io
import json
import os
import shutil
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

import archive  # noqa: E402
import manifest as manifest_mod  # noqa: E402
import shortname  # noqa: E402

EXPECTED_TABLE = Path(__file__).resolve().parent / "expected_dry_run.txt"

# The only statuses the schema defines (CLAUDE.md, "Manifest schema"), plus
# `archived`, which archive.py writes on a successful move. An example file
# whose job is to document the schema must not invent a seventh.
VALID_STATUSES = frozenset({
    "pending_review", "approved", "archived", "rejected", "skipped",
    "download_failed",
})


def build_scratch_archive(tmp: Path, layout: dict, entries: dict) -> Path:
    """Create every folder the example set routes to, plus a stand-in staging
    file per entry that has a local_path. Returns the archive root."""
    archive_dir = tmp / "archive"
    special = layout["special_folders"]

    dirs = [
        special["crossover"],
        special["others_oc"],
        special["others_unknown_source"],
        special["others_known_series"],
        f"{special['wallpaper_root']}/{special['wallpaper_pc']}",
        f"{special['wallpaper_root']}/{special['wallpaper_phone']}",
    ]
    for name, fdef in layout["franchises"].items():
        dirs.append(name)
        for character in fdef.get("characters", []):
            dirs.append(f"{name}/{character}")
        if fdef["style"] == "nested":
            dirs.append(f"{name}/{layout['group_subfolder']}")
    for d in dirs:
        (archive_dir / d).mkdir(parents=True, exist_ok=True)

    for entry in entries.values():
        if "local_path" not in entry:
            continue
        staged = tmp / entry["local_path"]
        staged.parent.mkdir(parents=True, exist_ok=True)
        staged.write_bytes(b"placeholder")
    return archive_dir


class ExampleConfigTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.layout = shortname.load_layout(REPO / "layout.example.json")
        cls.manifest = manifest_mod.load_manifest(REPO / "manifest.example.json")

        # The shortname file is referenced by name from layout.json; point at the
        # committed example instead of whatever the developer's real one is.
        layout_for_shortnames = dict(cls.layout)
        layout_for_shortnames["shortname_file"] = str(
            REPO / "known_series_names.example.txt")
        cls.shortname_entries = shortname.load_shortname_map(layout_for_shortnames)
        cls.series_aliases = shortname.load_series_aliases(
            REPO / "data" / "series_aliases.example.json")

        cls._tmp = tempfile.mkdtemp(prefix="oshiire-test-")
        tmp = Path(cls._tmp)
        cls.archive_dir = build_scratch_archive(tmp, cls.layout, cls.manifest)

        # plan_moves resolves local_path relative to the cwd.
        cls._cwd = os.getcwd()
        os.chdir(tmp)
        cls.rows, cls.skipped = archive.plan_moves(
            cls.manifest, cls.layout, cls.shortname_entries,
            cls.archive_dir, cls.series_aliases)

    @classmethod
    def tearDownClass(cls):
        os.chdir(cls._cwd)
        shutil.rmtree(cls._tmp, ignore_errors=True)

    # ---------------------------------------------------------------- schema
    def test_no_pseudo_entries(self):
        """Every top-level key must be a real entry.

        A manifest is iterated by every stage, so a documentation-only key is a
        pseudo-entry that each consumer has to skip by accident rather than by
        design. Guards the fix for exactly that.
        """
        for key, entry in self.manifest.items():
            self.assertFalse(
                key.startswith("_"),
                f"{key!r} is a pseudo-entry; the manifest has no comment slot")
            self.assertIsInstance(
                entry, dict, f"{key!r} must be a dict -- consumers call .get()")
            self.assertIn("status", entry, f"{key!r} has no status")

    def test_only_documented_statuses(self):
        found = {e["status"] for e in self.manifest.values()}
        invented = found - VALID_STATUSES
        self.assertEqual(set(), invented, f"undocumented status(es): {invented}")
        missing = VALID_STATUSES - found
        self.assertEqual(
            set(), missing,
            f"example no longer demonstrates status(es): {missing}")

    def test_character_guess_is_never_unknown(self):
        """[] means 'nothing identified'. A placeholder string would be
        indistinguishable from a real name to folder matching and to the
        group-vs-single character count."""
        saw_empty = False
        for key, entry in self.manifest.items():
            names = entry.get("character_guess")
            if names is None:
                continue
            self.assertIsInstance(names, list, f"{key}: character_guess must be a list")
            self.assertNotIn("Unknown", names, f"{key}: 'Unknown' placeholder")
            saw_empty = saw_empty or names == []
        self.assertTrue(
            saw_empty, "no entry demonstrates the empty character_guess rule")

    def test_gallery_entries_share_a_parent_post_id(self):
        gallery = {k: v for k, v in self.manifest.items() if v.get("image_index")}
        self.assertEqual(2, len(gallery), "expected exactly one gallery pair")
        self.assertEqual(
            1, len({v["post_id"] for v in gallery.values()}),
            "gallery siblings must share one parent post_id")
        for key, entry in gallery.items():
            self.assertNotEqual(
                key, entry["post_id"],
                f"{key}: gallery dict key must differ from the parent post_id")

    # ---------------------------------------------------------------- routing
    def test_dry_run_plan_matches_committed_table(self):
        buf = io.StringIO()
        with redirect_stdout(buf):
            archive.print_table(self.rows)
        actual = buf.getvalue().strip().replace("\r\n", "\n")
        expected = EXPECTED_TABLE.read_text(encoding="utf-8").strip().replace("\r\n", "\n")
        self.assertEqual(
            expected, actual,
            "\n\nThe example configs no longer route as recorded.\n"
            "If the change was intended, refresh the fixture:\n"
            "    python tests/test_example_configs.py --update\n")

    def test_nothing_flags(self):
        flagged = [r for r in self.rows if r["outcome"] == "flag"]
        self.assertEqual(
            [], [(r["post_id"], r["note"]) for r in flagged],
            "the example set must route cleanly -- a flag here means the "
            "templates and layout.example.json disagree")

    def test_every_routing_case_is_exercised(self):
        """The example set is the router's documentation, so it has to keep
        covering every branch of the precedence ladder."""
        dests = [r["dest_display"] for r in self.rows if r["outcome"] == "move"]
        notes = " ".join(r["note"] for r in self.rows)
        special = self.layout["special_folders"]

        def any_dest(prefix):
            return any(d.startswith(prefix) for d in dests)

        self.assertTrue(any_dest("Neon Ward/"), "flat style not exercised")
        self.assertTrue(any_dest("Starfall Chronicle/Sera/"), "nested style not exercised")
        self.assertTrue(
            any_dest(f"Starfall Chronicle/{self.layout['group_subfolder']}/"),
            "Others_Group not exercised")
        self.assertTrue(any_dest("Lantern District/"), "nested fallback:root not exercised")
        self.assertTrue(any_dest(special["crossover"] + "/"), "crossover not exercised")
        self.assertTrue(any_dest(special["others_known_series"] + "/"), "shortname not exercised")
        self.assertTrue(any_dest(special["others_oc"] + "/"), "Others/OC not exercised")
        self.assertTrue(
            any_dest(special["others_unknown_source"] + "/"),
            "Others/Unknown Sauce not exercised")
        self.assertIn("alias", notes, "franchise alias resolution not exercised")
        self.assertIn("shortname", notes, "shortname fallback note not exercised")

        copies = {r["copy_target"] for r in self.rows if r["outcome"] == "copy"}
        self.assertEqual({"pc", "phone"}, copies, "wallpaper copies not exercised")

        suffixed = [d for d in dests if "_VI." in d or "_NW." in d]
        self.assertTrue(suffixed, "shortname filename suffix not exercised")

    def test_wallpaper_copy_follows_its_move(self):
        """apply_moves processes rows in order and a copy's source is the
        primary move's destination, so a copy row must never precede it."""
        seen_move = set()
        for row in self.rows:
            if row["outcome"] == "move":
                seen_move.add(row["post_id"])
            elif row["outcome"] == "copy":
                self.assertIn(
                    row["post_id"], seen_move,
                    f"{row['post_id']}: wallpaper copy planned before its move")


def _update_fixture():
    """Regenerate expected_dry_run.txt from the current example configs."""
    ExampleConfigTests.setUpClass()
    try:
        buf = io.StringIO()
        with redirect_stdout(buf):
            archive.print_table(ExampleConfigTests.rows)
        EXPECTED_TABLE.write_text(buf.getvalue().strip() + "\n", encoding="utf-8")
        print(f"Wrote {EXPECTED_TABLE}")
        print(buf.getvalue())
    finally:
        ExampleConfigTests.tearDownClass()


if __name__ == "__main__":
    if "--update" in sys.argv:
        _update_fixture()
    else:
        unittest.main(verbosity=2)
