"""Tests for the browser-mode idle auto-shutdown (review.py).

Closing the tab in browser mode leaves the server running with nobody watching;
the open page pings a heartbeat and a watchdog shuts the server down cleanly once
the page has been quiet for IDLE_SHUTDOWN_SECONDS with no job running. The clock
is faked throughout -- no test ever waits a real second.

    python -m unittest discover -s tests
    python tests/test_idle_shutdown.py
"""
import sys
import unittest
from pathlib import Path
from unittest import mock

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "src"))

from review_sandbox import ReviewSandbox  # noqa: E402

# review binds its manifest/layout/archive dir at import, and resolve exits when
# there's no layout.json: import a private copy inside a throwaway data root,
# and run this module's tests inside it too.
_sandbox = ReviewSandbox()
review = _sandbox.load()
setUpModule = _sandbox.activate
tearDownModule = _sandbox.close


class IdleExpiredTest(unittest.TestCase):
    """_idle_expired(now, last, job_running, limit) -- the pure watchdog
    decision, exercised entirely on injected clock values."""

    LIMIT = review.IDLE_SHUTDOWN_SECONDS

    def test_expires_once_quiet_past_the_limit(self):
        self.assertTrue(review._idle_expired(self.LIMIT + 1, 0.0, False))
        self.assertTrue(review._idle_expired(self.LIMIT, 0.0, False))  # exactly at

    def test_stays_alive_within_the_window(self):
        self.assertFalse(review._idle_expired(self.LIMIT - 1, 0.0, False))

    def test_never_expires_while_a_job_runs(self):
        # Even hours past the limit, a running job keeps the server up.
        self.assertFalse(review._idle_expired(self.LIMIT * 100, 0.0, True))

    def test_no_heartbeat_yet_keeps_alive(self):
        self.assertFalse(review._idle_expired(10_000.0, None, False))

    def test_limit_is_overridable(self):
        self.assertTrue(review._idle_expired(50.0, 0.0, False, limit=30))
        self.assertFalse(review._idle_expired(20.0, 0.0, False, limit=30))


class NoteHeartbeatTest(unittest.TestCase):
    def setUp(self):
        self._prev = review._last_heartbeat

    def tearDown(self):
        review._last_heartbeat = self._prev

    def test_stamps_the_injected_clock(self):
        review._note_heartbeat(now=1234.5)
        self.assertEqual(review._last_heartbeat, 1234.5)

    def test_default_uses_the_monotonic_clock(self):
        with mock.patch.object(review.time, "monotonic", return_value=999.0):
            review._note_heartbeat()
        self.assertEqual(review._last_heartbeat, 999.0)


class IdleWatchdogTest(unittest.TestCase):
    """The watchdog loop on a fake clock: one iteration, no real sleep, and it
    asks for the shutdown exactly when the pure decision says to."""

    def setUp(self):
        self._prev = review._last_heartbeat

    def tearDown(self):
        review._last_heartbeat = self._prev

    def test_shuts_down_when_idle_and_no_job(self):
        review._last_heartbeat = 0.0
        with mock.patch.object(review.time, "sleep"), \
             mock.patch.object(review.time, "monotonic", return_value=10_000.0), \
             mock.patch.object(review.jobs, "running", return_value=None), \
             mock.patch.object(review, "_schedule_server_shutdown") as shutdown:
            review._idle_watchdog()
        shutdown.assert_called_once_with(delay=0)

    def test_does_not_shut_down_while_a_job_runs(self):
        # A running job never expires, so the loop would spin forever; cap it by
        # making the second sleep raise, and assert no shutdown was requested.
        review._last_heartbeat = 0.0
        with mock.patch.object(review.time, "sleep", side_effect=[None, StopIteration]), \
             mock.patch.object(review.time, "monotonic", return_value=10_000.0), \
             mock.patch.object(review.jobs, "running", return_value="Ingest"), \
             mock.patch.object(review, "_schedule_server_shutdown") as shutdown:
            with self.assertRaises(StopIteration):
                review._idle_watchdog()
        shutdown.assert_not_called()

    def test_one_idle_check_is_not_enough(self):
        # First idle check must NOT shut down; cap the loop after two sleeps so
        # the test can't spin, and assert still no shutdown after a single idle.
        with mock.patch.object(review.time, "sleep", side_effect=[None, StopIteration]), \
             mock.patch.object(review, "_idle_expired", side_effect=[True, True]), \
             mock.patch.object(review.jobs, "running", return_value=None), \
             mock.patch.object(review.time, "monotonic", return_value=0.0), \
             mock.patch.object(review, "_schedule_server_shutdown") as shutdown:
            # Two idle checks in a row WOULD shut down on the 2nd, but the 2nd
            # sleep raises first -- so exactly one idle check has happened.
            with self.assertRaises(StopIteration):
                review._idle_watchdog()
        shutdown.assert_not_called()

    def test_two_consecutive_idle_checks_shut_down(self):
        with mock.patch.object(review.time, "sleep"), \
             mock.patch.object(review, "_idle_expired", side_effect=[True, True]), \
             mock.patch.object(review.jobs, "running", return_value=None), \
             mock.patch.object(review.time, "monotonic", return_value=0.0), \
             mock.patch.object(review, "_schedule_server_shutdown") as shutdown:
            review._idle_watchdog()
        shutdown.assert_called_once_with(delay=0)

    def test_a_heartbeat_between_checks_resets_the_streak(self):
        # idle, then a heartbeat arrives (not idle) -> streak resets; the third
        # sleep raises before a second *consecutive* idle can occur.
        with mock.patch.object(review.time, "sleep", side_effect=[None, None, StopIteration]), \
             mock.patch.object(review, "_idle_expired", side_effect=[True, False, True]), \
             mock.patch.object(review.jobs, "running", return_value=None), \
             mock.patch.object(review.time, "monotonic", return_value=0.0), \
             mock.patch.object(review, "_schedule_server_shutdown") as shutdown:
            with self.assertRaises(StopIteration):
                review._idle_watchdog()
        shutdown.assert_not_called()


if __name__ == "__main__":
    unittest.main(verbosity=2)
