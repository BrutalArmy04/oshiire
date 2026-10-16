"""The Preview filing plan table's data path: archive.py's --json plan rows and
jobs.py's reader for them.

The UI renders the dry run as a real table from these STRUCTURED rows rather than
by parsing the padded text table. These tests pin that contract end to end:
archive.plan_display_rows / print_plan_json produce the rows (needs-Resolve
first), and jobs.parse_plan_row reads exactly the lines print_plan_json writes
and ignores everything else.

    python -m unittest tests.test_plan_table
"""
import io
import sys
import time
import unittest
from contextlib import redirect_stdout
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

import archive
import jobs


def _row(post_id, outcome, *, franchise=(), characters=(), dest="-", copy_target=None):
    row = {
        "post_id": post_id,
        "outcome": outcome,
        "franchise": list(franchise),
        "characters": list(characters),
        "dest_display": dest,
    }
    if copy_target is not None:
        row["copy_target"] = copy_target
    return row


class PlanStatusTest(unittest.TestCase):
    def test_each_outcome_maps_to_a_human_status(self):
        self.assertEqual(archive._plan_status(_row("a", "move")), "move")
        self.assertEqual(archive._plan_status(_row("a", "flag")), "needs Resolve")
        self.assertEqual(archive._plan_status(_row("a", "missing_file")), "missing file")
        self.assertEqual(
            archive._plan_status(_row("a", "copy_missing_directory")),
            "wallpaper copy (needs folder)")

    def test_wallpaper_copy_names_its_target(self):
        self.assertEqual(
            archive._plan_status(_row("a", "copy", copy_target="pc")),
            "wallpaper copy (pc)")
        self.assertEqual(
            archive._plan_status(_row("a", "copy", copy_target="phone")),
            "wallpaper copy (phone)")


class PlanDisplayRowsTest(unittest.TestCase):
    def test_needs_resolve_rows_come_first_order_preserved_within(self):
        rows = [
            _row("t3_move1", "move", franchise=["Genshin Impact"],
                 characters=["Ayaka"], dest="Genshin Impact/Ayaka/x.jpg"),
            _row("t3_flagA", "flag", franchise=["Neon Ward"]),
            _row("t3_move2", "move", dest="Neon Ward/y.jpg"),
            _row("t3_flagB", "flag"),
        ]
        out = archive.plan_display_rows(rows)
        # Flags first (in their original relative order), then the rest in order.
        self.assertEqual([r["image"] for r in out],
                         ["t3_flagA", "t3_flagB", "t3_move1", "t3_move2"])
        self.assertTrue(out[0]["needs_resolve"])
        self.assertFalse(out[-1]["needs_resolve"])

    def test_row_shape_and_joins(self):
        rows = [_row("t3_grp", "move", franchise=["Honkai", "Genshin Impact"],
                     characters=["Acheron", "Raiden Shogun Ei"],
                     dest="Crossover/z.png")]
        [row] = archive.plan_display_rows(rows)
        self.assertEqual(row["image"], "t3_grp")
        self.assertEqual(row["franchise"], "Honkai, Genshin Impact")
        self.assertEqual(row["character"], "Acheron, Raiden Shogun Ei")
        self.assertEqual(row["destination"], "Crossover/z.png")
        self.assertEqual(row["status"], "move")

    def test_empty_lists_render_as_dash(self):
        [row] = archive.plan_display_rows([_row("t3_x", "flag")])
        self.assertEqual(row["franchise"], "-")
        self.assertEqual(row["character"], "-")
        self.assertEqual(row["destination"], "-")


class PrintPlanJsonRoundTripTest(unittest.TestCase):
    """print_plan_json writes exactly the lines jobs.parse_plan_row reads back."""

    def test_round_trip_through_jobs_parser(self):
        rows = [
            _row("t3_flag", "flag", franchise=["Re:Zero"]),
            _row("t3_move", "move", franchise=["Genshin Impact"],
                 characters=["Hu Tao"], dest="Genshin Impact/押入れ/a.jpg"),
            _row("t3_wp", "copy", copy_target="pc", dest="Wallpaper/PC/a.jpg"),
        ]
        buf = io.StringIO()
        with redirect_stdout(buf):
            archive.print_plan_json(rows)
        lines = buf.getvalue().splitlines()
        parsed = [jobs.parse_plan_row(line) for line in lines]
        self.assertEqual(parsed, archive.plan_display_rows(rows))
        # Non-ASCII (the Japanese folder name) survives the JSON round trip.
        self.assertTrue(any("押入れ" in p["destination"] for p in parsed))


# A realistic worst case: long franchise names and long archive paths, the shape
# of the real data (the table was first checked only against the ~12-row example
# sandbox). Used by the scale fixture below and by the UI render measurement.
_LONG_FRANCHISE = "Grand Celestial Chronicle of the Eternal Radiant Dominion XVII"
_LONG_CHARACTER = "Seraphine Valentina Aldercrest the Third"


def make_bulk_rows(n):
    """n plan rows shaped like real data: long franchise names, long character
    names, and long nested archive paths. Every ~11th row is a flag so the
    needs-Resolve ordering is exercised at scale too."""
    rows = []
    for i in range(n):
        outcome = "flag" if i % 11 == 0 else "move"
        dest = ("-" if outcome == "flag"
                else f"{_LONG_FRANCHISE}/{_LONG_CHARACTER}/"
                     f"bulk_{i:05d}_a_rather_long_source_filename_from_reddit.jpg")
        rows.append(_row(f"t3_bulk{i:05d}", outcome,
                         franchise=[_LONG_FRANCHISE], characters=[_LONG_CHARACTER],
                         dest=dest))
    return rows


class PlanScaleTest(unittest.TestCase):
    """The data path at real scale (~1000 rows, long names/paths) -- correct and
    fast. The ~12-row example sandbox never exercised this."""

    N = 1000

    def test_thousand_rows_round_trip_correct_and_fast(self):
        rows = make_bulk_rows(self.N)
        start = time.perf_counter()
        buf = io.StringIO()
        with redirect_stdout(buf):
            archive.print_plan_json(rows)
        lines = buf.getvalue().splitlines()
        parsed = [jobs.parse_plan_row(line) for line in lines]
        elapsed = time.perf_counter() - start

        self.assertEqual(len(parsed), self.N)
        self.assertEqual(parsed, archive.plan_display_rows(rows))
        # Needs-Resolve rows are all first; the long strings survive intact.
        flags = sum(1 for p in parsed if p["needs_resolve"])
        self.assertEqual([p["needs_resolve"] for p in parsed[:flags]], [True] * flags)
        self.assertTrue(all(not p["needs_resolve"] for p in parsed[flags:]))
        self.assertEqual(parsed[-1]["franchise"], _LONG_FRANCHISE)
        self.assertIn(_LONG_CHARACTER, parsed[-1]["destination"])
        # Pure data path for 1000 rows is milliseconds; 1s is a huge, non-flaky
        # ceiling that still catches an accidental O(n^2).
        self.assertLess(elapsed, 1.0, f"1000-row data path took {elapsed:.3f}s")


class ParsePlanRowTest(unittest.TestCase):
    def test_valid_line(self):
        line = archive.PLAN_PREFIX + '{"image": "t3_a", "status": "move"}'
        self.assertEqual(jobs.parse_plan_row(line), {"image": "t3_a", "status": "move"})

    def test_ignores_non_plan_lines(self):
        self.assertIsNone(jobs.parse_plan_row("OSHIIRE_SUMMARY moves=1 flagged=0"))
        self.assertIsNone(jobs.parse_plan_row("just a log line"))
        self.assertIsNone(jobs.parse_plan_row(""))

    def test_malformed_json_is_ignored_not_raised(self):
        self.assertIsNone(jobs.parse_plan_row(archive.PLAN_PREFIX + "{not json"))
        # A JSON scalar (not an object) is not a row.
        self.assertIsNone(jobs.parse_plan_row(archive.PLAN_PREFIX + '"a string"'))


if __name__ == "__main__":
    unittest.main(verbosity=2)
