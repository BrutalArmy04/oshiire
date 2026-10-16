"""startup_timing: the per-phase startup clock whose lines land in
data/oshiire.log. Pure stdlib; these tests drive it with a captured logger and a
controlled clock so they never depend on real wall time.

    python -m unittest tests.test_startup_timing
"""
import sys
import unittest
from pathlib import Path
from unittest import mock

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

import startup_timing


class StartupTimingTest(unittest.TestCase):
    def setUp(self):
        self.logged = []

    def _log(self, line):
        self.logged.append(line)

    def test_reset_clears_phases(self):
        startup_timing.reset(t0=0.0)
        self.assertEqual(startup_timing.phases(), [])

    def test_marks_accumulate_with_monotonic_totals(self):
        # Drive the clock: reset at 0, then marks at 1.0, 1.5, 4.0 seconds.
        with mock.patch.object(startup_timing.time, "monotonic",
                               side_effect=[0.0, 1.0, 1.5, 4.0]):
            startup_timing.reset()
            startup_timing.mark("imports", log=self._log)
            startup_timing.mark("manifest load", log=self._log)
            startup_timing.mark("server up", log=self._log)
        phases = startup_timing.phases()
        self.assertEqual([p for p, _, _ in phases],
                         ["imports", "manifest load", "server up"])
        # Deltas are between consecutive marks; totals are since t0 and rise.
        self.assertAlmostEqual(phases[0][1], 1.0)   # imports delta
        self.assertAlmostEqual(phases[1][1], 0.5)   # manifest load delta
        self.assertAlmostEqual(phases[2][1], 2.5)   # server up delta
        self.assertEqual([round(t, 3) for _, _, t in phases], [1.0, 1.5, 4.0])
        self.assertEqual(len(self.logged), 3)
        self.assertIn("imports", self.logged[0])

    def test_mark_returns_delta_and_total(self):
        with mock.patch.object(startup_timing.time, "monotonic",
                               side_effect=[0.0, 2.0]):
            startup_timing.reset()
            delta, total = startup_timing.mark("imports", log=self._log)
        self.assertAlmostEqual(delta, 2.0)
        self.assertAlmostEqual(total, 2.0)

    def test_format_breakdown_lists_phases_and_total(self):
        with mock.patch.object(startup_timing.time, "monotonic",
                               side_effect=[0.0, 1.0, 3.0]):
            startup_timing.reset()
            startup_timing.mark("imports", log=self._log)
            startup_timing.mark("Blocks build", log=self._log)
        out = startup_timing.format_breakdown()
        self.assertIn("imports", out)
        self.assertIn("Blocks build", out)
        self.assertIn("TOTAL", out)
        self.assertIn("3.00s", out)  # the last phase's total is the grand total

    def test_format_breakdown_empty(self):
        startup_timing.reset()
        self.assertIn("no phases", startup_timing.format_breakdown())

    def test_mark_never_raises_if_logger_raises(self):
        def boom(_):
            raise RuntimeError("log sink is gone")
        startup_timing.reset()
        # Must not propagate -- timing is instrumentation, not critical path.
        startup_timing.mark("imports", log=boom)
        self.assertEqual([p for p, _, _ in startup_timing.phases()], ["imports"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
