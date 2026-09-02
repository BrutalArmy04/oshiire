"""Unit tests for hash_index.py's row-level index writers.

`build` is the source of truth for the pHash index, but it re-walks and
re-hashes the whole archive, which is minutes of work for a fact that is
already known. Three small writers keep the index current between sweeps:

  - record_indexed_file  (pre-existing) -- archive.py, as a file is filed
  - move_indexed_file    -- sync.py, when a filed image is moved by hand
  - remove_indexed_file  -- for a file that is gone; reported, never auto-run
  - get_indexed_rel_paths -- read-back helper for callers and for these tests

What these pin down, in the order the bugs would bite:

  1. move_indexed_file RE-KEYS rather than re-inserting. The row carries a
     phash that cost ~0.2s to compute; losing it would silently downgrade the
     move into "rebuild it later", which is the stale-index failure this
     whole mechanism exists to prevent.
  2. The primary-key conflict path DELETES the old row instead of failing.
     `rel_path` is the PK, so an UPDATE onto an occupied path raises; the
     occupant is a row a later `build` (or archive run) wrote from the file
     as it actually sits, so it is the one that must survive.
  3. Every writer is best-effort in record_indexed_file's exact sense: an
     absent db is False, never a conjured one-row index; an absent row is a
     clean False, not an exception. Callers reach these AFTER files have
     already moved, so raising would strand the manifest write.

Runs on a throwaway SQLite index in a temp dir. No image is ever opened (the
hashes below are literals), and neither the real index nor ARCHIVE_DIR is
read or written.

    python -m unittest discover -s tests
    python tests/test_hash_index.py
"""
import shutil
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from hash_index import (  # noqa: E402
    DEFAULT_HASH_SIZE,
    get_indexed_rel_paths,
    move_indexed_file,
    open_index,
    record_indexed_file,
    remove_indexed_file,
)

# 16 hex characters == 64 bits, which is what open_index stores as hash_bits at
# the default hash size. record_indexed_file checks the hash's own length
# against the index's depth, so these have to be the right width.
HASH_A = "0f1e2d3c4b5a6978"
HASH_B = "ffeeddccbbaa9988"


class IndexWriterTestCase(unittest.TestCase):
    """Shared throwaway index."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="oshiire-hashidx-"))
        self.db = self.tmp / "index.db"
        open_index(self.db, DEFAULT_HASH_SIZE).close()

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _seed(self, rel_path: str, phash: str = HASH_A) -> None:
        self.assertTrue(
            record_indexed_file(self.db, rel_path, phash, 1234, 1700000000.0, 800, 1200),
            f"failed to seed {rel_path}",
        )

    def _row(self, rel_path: str):
        conn = sqlite3.connect(self.db)
        try:
            return conn.execute(
                "SELECT phash, size, mtime, width, height FROM images WHERE rel_path = ?",
                (rel_path,),
            ).fetchone()
        finally:
            conn.close()


class MoveIndexedFileTest(IndexWriterTestCase):
    def test_renames_the_key_and_keeps_the_row(self):
        self._seed("Genshin Impact/Fatui/t3_a.jpg")

        moved = move_indexed_file(
            self.db, "Genshin Impact/Fatui/t3_a.jpg", "Genshin Impact/Sandrone/t3_a.jpg"
        )

        self.assertTrue(moved)
        self.assertEqual(
            get_indexed_rel_paths(self.db), {"Genshin Impact/Sandrone/t3_a.jpg"}
        )
        # The expensive part of the row -- the hash and the dimensions -- has to
        # survive, otherwise this is just a slower delete.
        self.assertEqual(
            self._row("Genshin Impact/Sandrone/t3_a.jpg"),
            (HASH_A, 1234, 1700000000.0, 800, 1200),
        )

    def test_conflicting_destination_deletes_the_old_row(self):
        self._seed("Genshin Impact/Fatui/t3_b.jpg", HASH_A)
        self._seed("Genshin Impact/Sandrone/t3_b.jpg", HASH_B)

        moved = move_indexed_file(
            self.db, "Genshin Impact/Fatui/t3_b.jpg", "Genshin Impact/Sandrone/t3_b.jpg"
        )

        self.assertTrue(moved)
        self.assertEqual(
            get_indexed_rel_paths(self.db), {"Genshin Impact/Sandrone/t3_b.jpg"}
        )
        # The row already at the destination was written from the file where it
        # actually sits, so it is the one that reflects reality.
        self.assertEqual(self._row("Genshin Impact/Sandrone/t3_b.jpg")[0], HASH_B)

    def test_absent_source_is_a_clean_no_op(self):
        self._seed("Genshin Impact/Fatui/t3_c.jpg")
        before = get_indexed_rel_paths(self.db)

        moved = move_indexed_file(self.db, "Nowhere/t3_zz.jpg", "Genshin Impact/Signora/t3_zz.jpg")

        self.assertFalse(moved)
        self.assertEqual(get_indexed_rel_paths(self.db), before)

    def test_identical_paths_are_a_no_op(self):
        self._seed("Genshin Impact/Fatui/t3_d.jpg")

        self.assertFalse(
            move_indexed_file(
                self.db, "Genshin Impact/Fatui/t3_d.jpg", "Genshin Impact/Fatui/t3_d.jpg"
            )
        )
        self.assertEqual(get_indexed_rel_paths(self.db), {"Genshin Impact/Fatui/t3_d.jpg"})

    def test_missing_database_is_never_created(self):
        absent = self.tmp / "no-such-index.db"

        self.assertFalse(move_indexed_file(absent, "a/x.jpg", "b/x.jpg"))
        self.assertFalse(absent.exists())

    def test_empty_paths_are_refused(self):
        self._seed("Genshin Impact/Fatui/t3_e.jpg")

        self.assertFalse(move_indexed_file(self.db, "", "Genshin Impact/Sandrone/t3_e.jpg"))
        self.assertFalse(move_indexed_file(self.db, "Genshin Impact/Fatui/t3_e.jpg", ""))
        self.assertEqual(get_indexed_rel_paths(self.db), {"Genshin Impact/Fatui/t3_e.jpg"})


class RemoveIndexedFileTest(IndexWriterTestCase):
    def test_removes_one_row(self):
        self._seed("Genshin Impact/Fatui/t3_f.jpg")
        self._seed("Genshin Impact/Sandrone/t3_g.jpg")

        self.assertTrue(remove_indexed_file(self.db, "Genshin Impact/Fatui/t3_f.jpg"))
        self.assertEqual(
            get_indexed_rel_paths(self.db), {"Genshin Impact/Sandrone/t3_g.jpg"}
        )

    def test_absent_row_is_false_not_an_error(self):
        self._seed("Genshin Impact/Fatui/t3_h.jpg")

        self.assertFalse(remove_indexed_file(self.db, "Nowhere/t3_zz.jpg"))
        self.assertEqual(get_indexed_rel_paths(self.db), {"Genshin Impact/Fatui/t3_h.jpg"})

    def test_missing_database_is_never_created(self):
        absent = self.tmp / "no-such-index.db"

        self.assertFalse(remove_indexed_file(absent, "a/x.jpg"))
        self.assertFalse(absent.exists())


class GetIndexedRelPathsTest(IndexWriterTestCase):
    def test_returns_every_key(self):
        self._seed("Crossover/t3_i.jpg")
        self._seed("Others/Known Series/t3_j_BtR.jpg")

        self.assertEqual(
            get_indexed_rel_paths(self.db),
            {"Crossover/t3_i.jpg", "Others/Known Series/t3_j_BtR.jpg"},
        )

    def test_empty_index_is_an_empty_set(self):
        self.assertEqual(get_indexed_rel_paths(self.db), set())

    def test_missing_database_is_an_empty_set(self):
        # Indistinguishable from an empty index on purpose: a user who has
        # never run `build` must not be a special case for any caller.
        self.assertEqual(get_indexed_rel_paths(self.tmp / "no-such-index.db"), set())


if __name__ == "__main__":
    unittest.main(verbosity=2)
