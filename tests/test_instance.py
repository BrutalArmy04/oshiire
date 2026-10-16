"""Tests for instance.py -- the single-instance lock shared by app.py (the
desktop window) and `python review.py` (the browser UI).

The lock is exercised with REAL second processes, since an OS advisory lock is
only meaningfully contended across processes (a second acquire in the same
process can re-take its own lock on some platforms). Nothing here touches the
real data/ -- every lock path lives under a throwaway temp dir.

    python -m unittest discover -s tests
    python tests/test_instance.py
"""
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

import instance


# A tiny program that tries to take the lock and reports the result. Run as a
# genuine second PROCESS so the OS lock is actually contended.
_LOCK_PROBE = """\
import sys
sys.path.insert(0, {repo!r})
import instance
h = instance.acquire_single_instance({path!r})
print("GOT" if h is not None else "NONE")
"""


# A holder program: takes the lock, announces it, then stays alive holding it
# until killed. Readiness is reported on stdout so the test never races the
# acquire.
_LOCK_HOLDER = """\
import sys, time
sys.path.insert(0, {repo!r})
import instance
h = instance.acquire_single_instance({path!r})
sys.stdout.write("LOCKED\\n" if h is not None else "FAIL\\n")
sys.stdout.flush()
time.sleep(600)
"""


# A holder that also RECORDS a port into the lock file, then holds the lock.
# Used to prove a second process can read the running instance's port (and PID)
# while the lock is still held (the "already running -> open its port / stop its
# process" path). It prints its own PID so the reader can assert the recorded
# PID matches the real holder.
_PORT_HOLDER = """\
import os, sys, time
sys.path.insert(0, {repo!r})
import instance
h = instance.acquire_single_instance({path!r})
if h is None:
    sys.stdout.write("FAIL\\n"); sys.stdout.flush(); sys.exit(1)
instance.record_port({port}, {path!r})
sys.stdout.write("LOCKED %d\\n" % os.getpid()); sys.stdout.flush()
time.sleep(600)
"""


class SingleInstanceTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="instance-lock-"))
        self.lock = self.tmp / "oshiire.lock"

    def _probe(self):
        script = _LOCK_PROBE.format(repo=str(REPO), path=str(self.lock))
        out = subprocess.run(
            [sys.executable, "-c", script],
            capture_output=True, text=True, timeout=30,
        )
        return out.stdout.strip()

    def test_second_process_is_locked_out_until_release(self):
        handle = instance.acquire_single_instance(self.lock)
        self.assertIsNotNone(handle)
        try:
            self.assertEqual(self._probe(), "NONE")  # contended while we hold it
        finally:
            handle.close()  # release
        self.assertEqual(self._probe(), "GOT")  # free again


class ReviewSingleInstanceTest(unittest.TestCase):
    """`python review.py` must refuse to start a second copy: when the single-
    instance lock is already held it prints the guidance and exits 1 instead of
    launching a rival Gradio server.

    A holder CHILD process takes the lock first; review then runs with its cwd
    set to the same sandbox, so it resolves the default data/oshiire.lock to the
    sandbox and contends with the holder -- never the real project data/. review
    reads its config relative to that cwd too, and its import aborts without a
    layout.json, so the sandbox is seeded with the committed layout.example.json
    (nothing else is required for the import to reach __main__)."""

    def test_review_exits_1_when_lock_held(self):
        sandbox = Path(tempfile.mkdtemp(prefix="review-lock-"))
        (sandbox / "layout.json").write_text(
            (REPO / "layout.example.json").read_text(encoding="utf-8"),
            encoding="utf-8",
        )
        lock = sandbox / "data" / "oshiire.lock"
        holder = subprocess.Popen(
            [sys.executable, "-c", _LOCK_HOLDER.format(repo=str(REPO), path=str(lock))],
            stdout=subprocess.PIPE, text=True,
        )
        try:
            # Block until the holder confirms it actually holds the lock, so the
            # review launch below is guaranteed to find it contended.
            self.assertEqual(holder.stdout.readline().strip(), "LOCKED")
            out = subprocess.run(
                [sys.executable, str(REPO / "review.py")],
                cwd=str(sandbox),
                capture_output=True, text=True, timeout=300,
            )
            self.assertEqual(out.returncode, 1, out.stderr)
            self.assertIn(
                "Oshiire is already open (close the other copy first).",
                out.stdout,
            )
        finally:
            holder.terminate()
            holder.wait(timeout=30)
            holder.stdout.close()


class PortRecordTest(unittest.TestCase):
    """record_port / read_recorded_port -- the lock file doubles as the record
    of the bound server port, so a second launch can open the running instance
    instead of failing."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="instance-port-"))
        self.lock = self.tmp / "oshiire.lock"

    def test_missing_file_reads_none(self):
        self.assertIsNone(instance.read_recorded_port(self.lock))

    def test_empty_file_reads_none(self):
        self.lock.write_text("", encoding="utf-8")
        self.assertIsNone(instance.read_recorded_port(self.lock))

    def test_garbage_reads_none(self):
        self.lock.write_text("not-a-port", encoding="utf-8")
        self.assertIsNone(instance.read_recorded_port(self.lock))

    def test_round_trip(self):
        instance.record_port(7863, self.lock)
        self.assertEqual(instance.read_recorded_port(self.lock), 7863)

    def test_round_trip_records_pid(self):
        # The default pid is this process's; read_lock_info returns both.
        instance.record_port(7863, self.lock)
        self.assertEqual(instance.read_lock_info(self.lock), (7863, os.getpid()))

    def test_round_trip_explicit_pid(self):
        instance.record_port(7863, self.lock, pid=424242)
        self.assertEqual(instance.read_lock_info(self.lock), (7863, 424242))

    def test_old_port_only_format_reads_pid_none(self):
        # A lock file left by an older version holds just the port.
        self.lock.write_text("7860", encoding="utf-8")
        self.assertEqual(instance.read_lock_info(self.lock), (7860, None))
        self.assertEqual(instance.read_recorded_port(self.lock), 7860)

    def test_empty_file_reads_info_none(self):
        self.lock.write_text("", encoding="utf-8")
        self.assertEqual(instance.read_lock_info(self.lock), (None, None))

    def test_missing_file_reads_info_none(self):
        self.assertEqual(instance.read_lock_info(self.lock), (None, None))

    def test_garbage_reads_info_none(self):
        self.lock.write_text("not-a-port", encoding="utf-8")
        self.assertEqual(instance.read_lock_info(self.lock), (None, None))

    def test_valid_port_garbage_pid_reads_pid_none(self):
        self.lock.write_text("7860 not-a-pid", encoding="utf-8")
        self.assertEqual(instance.read_lock_info(self.lock), (7860, None))

    def test_clear_recorded_port_empties(self):
        instance.record_port(7860, self.lock)
        instance.clear_recorded_port(self.lock)  # a fresh session owns the lock now
        self.assertIsNone(instance.read_recorded_port(self.lock))

    def test_clear_on_missing_file_is_a_no_op(self):
        self.assertFalse(self.lock.exists())
        instance.clear_recorded_port(self.lock)  # must not raise
        self.assertIsNone(instance.read_recorded_port(self.lock))

    def test_rewrite_replaces_previous_port(self):
        instance.record_port(7860, self.lock)
        instance.record_port(7899, self.lock)  # a later launch on a different port
        self.assertEqual(instance.read_recorded_port(self.lock), 7899)

    def test_second_process_reads_port_while_lock_held(self):
        """The crux: a DIFFERENT process must read the recorded port AND PID while
        the holder still owns the lock -- otherwise 'open the running port' and
        'stop its process' can't work. The holder locks + records in its own
        process; this test process reads it back."""
        holder = subprocess.Popen(
            [sys.executable, "-c",
             _PORT_HOLDER.format(repo=str(REPO), path=str(self.lock), port=7899)],
            stdout=subprocess.PIPE, text=True,
        )
        try:
            ready = holder.stdout.readline().split()
            self.assertEqual(ready[0], "LOCKED")
            holder_pid = int(ready[1])
            # The lock is genuinely held (a fresh acquire here is refused)...
            self.assertIsNone(instance.acquire_single_instance(self.lock))
            # ...yet the port is still readable from this other process.
            self.assertEqual(instance.read_recorded_port(self.lock), 7899)
            # ...and so is the holder's real PID (what 'Stop it' terminates).
            self.assertEqual(instance.read_lock_info(self.lock), (7899, holder_pid))
        finally:
            holder.terminate()
            holder.wait(timeout=30)
            holder.stdout.close()


if __name__ == "__main__":
    unittest.main(verbosity=2)
