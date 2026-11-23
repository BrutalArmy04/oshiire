"""Tests for runlock.py: one pipeline run at a time, never alongside an open app.

The lock contention is tested with REAL processes (a holder and a challenger
started with subprocess), because an OS file lock only means something across
processes. Everything runs against a temp folder: the lock files are
<tmp>/data/run.lock and <tmp>/data/oshiire.lock, and nothing here touches the
network, the real manifest or the real .env.

    python -m unittest tests.test_runlock
"""
import contextlib
import io
import os
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

REPO = Path(__file__).resolve().parent.parent
SRC = REPO / "src"
sys.path.insert(0, str(SRC))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import instance
import runlock
from config_sandbox import isolated_config

HOLDER_CODE = """
import os, sys
sys.path.insert(0, sys.argv[1])
import runlock
runlock.acquire(sys.argv[3], sys.argv[2])
print("READY", os.getpid(), flush=True)
sys.stdin.readline()
"""

GUARD_CODE = """
import sys
sys.path.insert(0, sys.argv[1])
import runlock
runlock.guard(sys.argv[3], sys.argv[2], sys.argv[4])
print("GOT THE LOCK", flush=True)
"""


class _Base(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="runlock-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        (self.tmp / "data").mkdir()
        self.run_lock = self.tmp / "data" / "run.lock"
        self.app_lock = self.tmp / "data" / "oshiire.lock"
        self.holders = []
        self.addCleanup(self._stop_holders)

    def _stop_holders(self):
        for proc in self.holders:
            if proc.poll() is None:
                proc.kill()
            proc.wait()
            for stream in (proc.stdin, proc.stdout):
                try:
                    stream.close()
                except OSError:
                    pass

    def kill_holder(self, proc, path=None):
        """Kill a holder the hard way and wait until the OS has really freed the lock.
        Kills the interpreter itself (Popen.kill() can hit only the venv launcher
        stub) and polls, since the release is not instantaneous."""
        path = path or self.run_lock
        try:
            os.kill(proc.real_pid, signal.SIGTERM)
        except OSError:
            pass
        proc.wait(timeout=30)
        deadline = time.time() + 15
        while runlock.holder(path) is not None:
            self.assertLess(time.time(), deadline, "the killed run still holds the lock")
            time.sleep(0.05)

    def hold(self, job="Ingest", path=None):
        """A separate process that takes the run lock and keeps it."""
        proc = subprocess.Popen(
            [sys.executable, "-c", HOLDER_CODE, str(SRC), str(path or self.run_lock), job],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True, cwd=str(self.tmp))
        self.holders.append(proc)
        word, pid = proc.stdout.readline().split()
        self.assertEqual(word, "READY")
        proc.real_pid = int(pid)  # Popen.pid can be a launcher stub; this is the interpreter
        return proc

    def challenge(self, job="Backfill", env_pid=None):
        """A separate process that calls runlock.guard(); (exit code, stdout, stderr)."""
        env = {k: v for k, v in os.environ.items() if k != runlock.APP_PID_ENV}
        if env_pid is not None:
            env[runlock.APP_PID_ENV] = str(env_pid)
        done = subprocess.run(
            [sys.executable, "-c", GUARD_CODE, str(SRC), str(self.run_lock), job, str(self.app_lock)],
            capture_output=True, text=True, cwd=str(self.tmp), env=env, timeout=60)
        return done.returncode, done.stdout, done.stderr

    def open_app(self, pid=None):
        """Stand in for the running app: hold the single-instance lock and record
        "port pid" in it, as review.launch_app does."""
        handle = instance.acquire_single_instance(self.app_lock)
        self.assertIsNotNone(handle)
        self.addCleanup(handle.close)
        instance.record_port(7860, self.app_lock, pid=os.getpid() if pid is None else pid)
        return handle


class RunLockContentionTest(_Base):
    def test_a_free_lock_is_taken(self):
        code, out, err = self.challenge()
        self.assertEqual((code, err), (0, ""))
        self.assertIn("GOT THE LOCK", out)

    def test_a_second_run_exits_1_and_says_who_has_it(self):
        holder = self.hold("Ingest")
        code, out, err = self.challenge("Backfill")
        self.assertEqual(code, 1)
        self.assertNotIn("GOT THE LOCK", out)
        match = re.fullmatch(
            r"Ingest is already running \(started (\d\d):(\d\d), process (\d+)\)\.\s*", err)
        self.assertIsNotNone(match, err)
        self.assertEqual(int(match.group(3)), holder.real_pid)
        clock = time.strftime("%H:%M", time.localtime())
        self.assertLessEqual(abs(int(match.group(1)) * 60 + int(match.group(2))
                                 - (int(clock[:2]) * 60 + int(clock[3:]))), 2)

    def test_holder_and_busy_message_describe_the_running_job(self):
        self.assertIsNone(runlock.holder(self.run_lock))
        self.assertIsNone(runlock.busy_message(self.run_lock))
        holder = self.hold("File images")
        job, pid, started = runlock.holder(self.run_lock)
        self.assertEqual((job, pid), ("File images", holder.real_pid))
        self.assertAlmostEqual(started, time.time(), delta=60)
        self.assertRegex(runlock.busy_message(self.run_lock),
                         rf"^File images is already running \(started \d\d:\d\d, process {holder.real_pid}\)\.$")

    def test_probing_never_takes_the_lock(self):
        holder = self.hold("Ingest")
        for _ in range(3):
            self.assertIsNotNone(runlock.holder(self.run_lock))
        holder.stdin.write("\n")
        holder.stdin.flush()
        holder.wait(timeout=30)
        deadline = time.time() + 15
        while runlock.holder(self.run_lock) is not None:
            self.assertLess(time.time(), deadline, "the finished run still holds the lock")
            time.sleep(0.05)
        self.assertEqual(self.challenge()[0], 0)

    def test_a_killed_run_frees_the_lock(self):
        holder = self.hold("Ingest")
        self.assertEqual(self.challenge()[0], 1)
        self.kill_holder(holder)
        self.assertIsNone(runlock.holder(self.run_lock))
        code, out, _ = self.challenge()
        self.assertEqual(code, 0)

    def test_in_process_second_acquire_is_refused_and_release_frees_it(self):
        handle = runlock.acquire("Ingest", self.run_lock)
        try:
            with self.assertRaises(runlock.AlreadyRunning) as ctx:
                # a second handle on the same file, as a second process would have
                other = runlock._try_lock(self.run_lock)
                if other is not None:
                    other.close()
                    self.fail("the lock was not exclusive")
                raise runlock.AlreadyRunning(*runlock._read_note(self.run_lock))
            self.assertEqual(ctx.exception.job, "Ingest")
            self.assertEqual(ctx.exception.pid, os.getpid())
        finally:
            runlock.release(handle)
        free = runlock._try_lock(self.run_lock)
        self.assertIsNotNone(free)
        free.close()

    def test_describe_without_a_readable_note(self):
        self.assertEqual(runlock.describe(None, None, None), "Another job is already running.")
        self.assertEqual(runlock.describe("Backfill", None, None), "Backfill is already running.")


class AppOpenTest(_Base):
    def test_a_cli_run_is_refused_while_the_app_is_open(self):
        self.open_app()
        code, out, err = self.challenge()
        self.assertEqual(code, 1)
        self.assertEqual(err.strip(), "Oshiire is open. Close it, or run this from the app.")
        self.assertNotIn("GOT THE LOCK", out)
        self.assertIsNone(runlock.holder(self.run_lock))   # and it did not take the lock

    def test_an_app_started_child_is_allowed(self):
        self.open_app()
        code, out, err = self.challenge(env_pid=os.getpid())
        self.assertEqual((code, err), (0, ""))
        self.assertIn("GOT THE LOCK", out)

    def test_a_pid_that_is_not_the_apps_is_refused(self):
        """No override: an environment variable only counts when its pid is the one
        the open app recorded in its own lock file."""
        self.open_app()
        for claimed in (os.getpid() + 1, 1, "not-a-number"):
            with self.subTest(claimed=claimed):
                code, _, err = self.challenge(env_pid=claimed)
                self.assertEqual(code, 1)
                self.assertIn("Oshiire is open", err)

    def test_an_app_started_child_still_cannot_start_a_second_run(self):
        self.open_app()
        self.hold("Ingest")
        code, _, err = self.challenge(env_pid=os.getpid())
        self.assertEqual(code, 1)
        self.assertIn("Ingest is already running", err)

    def test_the_variable_means_nothing_when_the_app_is_closed(self):
        code, out, _ = self.challenge(env_pid=12345)
        self.assertEqual(code, 0)                       # app closed: any CLI run may go
        self.assertIn("GOT THE LOCK", out)

    def test_app_is_open_reports_the_single_instance_lock(self):
        self.assertFalse(runlock.app_is_open(self.app_lock))
        self.open_app()
        self.assertTrue(runlock.app_is_open(self.app_lock))

    def test_started_by_app_reads_the_recorded_pid(self):
        self.open_app(pid=4321)
        with mock.patch.dict(os.environ, {runlock.APP_PID_ENV: "4321"}):
            self.assertTrue(runlock.started_by_app(self.app_lock))
        with mock.patch.dict(os.environ, {runlock.APP_PID_ENV: "4322"}):
            self.assertFalse(runlock.started_by_app(self.app_lock))
        with mock.patch.dict(os.environ):
            os.environ.pop(runlock.APP_PID_ENV, None)
            self.assertFalse(runlock.started_by_app(self.app_lock))


class WiringTest(_Base):
    """The scripts take the guard, and the app hands its children the pid."""

    SCRIPTS = {
        "ingest.py": 'runlock.guard("Ingest")',
        "backfill.py": 'runlock.guard("Backfill")',
        "ai_tag.py": 'runlock.guard("AI tagging")',
        "imagemeta.py": 'runlock.guard("Hashing")',
        "archive.py": 'runlock.guard("File images")',
    }

    def test_every_pipeline_script_takes_the_guard_in_main(self):
        for name, call in self.SCRIPTS.items():
            with self.subTest(script=name):
                source = (SRC / name).read_text(encoding="utf-8")
                main = source[source.index("def main("):]
                self.assertIn(call, main)

    def test_archive_only_locks_for_apply(self):
        source = (SRC / "archive.py").read_text(encoding="utf-8")
        self.assertRegex(source, r"if args\.apply:[^\n]*\n\s+runlock\.guard\(\"File images\"\)")

    def test_ingest_main_refuses_while_another_run_holds_the_lock(self):
        import ingest
        self.hold("Backfill")
        err = io.StringIO()
        with isolated_config(self.tmp), \
                mock.patch.object(sys, "argv", ["ingest.py"]), \
                mock.patch.object(ingest, "load_dotenv"), \
                mock.patch.object(ingest, "load_manifest") as load, \
                mock.patch.object(ingest.savedlisting, "fetch_page") as fetch, \
                contextlib.redirect_stderr(err):
            with self.assertRaises(SystemExit) as ctx:
                ingest.main()
        self.assertEqual(ctx.exception.code, 1)
        self.assertIn("Backfill is already running", err.getvalue())
        load.assert_not_called()
        fetch.assert_not_called()

    def test_ingest_main_refuses_while_the_app_is_open(self):
        import ingest
        self.open_app()
        err = io.StringIO()
        with isolated_config(self.tmp), \
                mock.patch.object(sys, "argv", ["ingest.py"]), \
                mock.patch.object(ingest, "load_dotenv"), \
                mock.patch.object(ingest, "load_manifest") as load, \
                contextlib.redirect_stderr(err):
            with self.assertRaises(SystemExit) as ctx:
                ingest.main()
        self.assertEqual(ctx.exception.code, 1)
        self.assertIn("Oshiire is open. Close it, or run this from the app.", err.getvalue())
        load.assert_not_called()

    def _run_ingest_cli(self, *argv):
        """ingest.main() with the given arguments, in the temp data root; returns
        (exit code or None, stdout, stderr) and the mock for load_manifest."""
        import ingest
        out, err = io.StringIO(), io.StringIO()
        code = None
        with isolated_config(self.tmp), \
                mock.patch.object(sys, "argv", ["ingest.py", *argv]), \
                mock.patch.object(ingest, "load_dotenv"), \
                mock.patch.object(ingest, "load_manifest", return_value={}) as load, \
                mock.patch.object(ingest, "reject_skipped") as reject, \
                mock.patch.object(ingest, "retry_skipped") as retry, \
                contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            try:
                ingest.main()
            except SystemExit as exc:
                code = exc.code
        return code, out.getvalue(), err.getvalue(), load, reject, retry

    def test_list_skipped_is_read_only_and_never_refused(self):
        """Another run holds the lock AND the app is open: listing still works."""
        self.hold("Backfill")
        self.open_app()
        code, out, err, load, _, _ = self._run_ingest_cli("--list-skipped")
        self.assertIsNone(code)
        self.assertEqual(err, "")
        self.assertIn("No skipped entries", out)
        load.assert_called_once()

    def test_retry_and_reject_skipped_stay_guarded(self):
        for argv, which in ((("--retry-skipped",), "retry"), (("--reject-skipped", "t3_x"), "reject")):
            with self.subTest(argv=argv):
                holder = self.hold("Backfill")
                code, _, err, load, reject, retry = self._run_ingest_cli(*argv)
                self.assertEqual(code, 1)
                self.assertIn("Backfill is already running", err)
                for mocked in (load, reject, retry):
                    mocked.assert_not_called()
                self.kill_holder(holder)

    def test_retry_and_reject_skipped_are_refused_while_the_app_is_open(self):
        self.open_app()
        for argv in (("--retry-skipped",), ("--reject-skipped", "t3_x")):
            with self.subTest(argv=argv):
                code, _, err, load, _, _ = self._run_ingest_cli(*argv)
                self.assertEqual(code, 1)
                self.assertIn("Oshiire is open", err)
                load.assert_not_called()

    def test_the_app_gives_its_children_its_own_pid(self):
        import jobs
        self.assertEqual(jobs._child_env()[runlock.APP_PID_ENV], str(os.getpid()))

    def test_the_menu_probe_names_the_running_job(self):
        import jobs
        (self.tmp / ".env").write_text(
            "REDDIT_USERNAME=some_user\nREDDIT_SESSION_COOKIE=reddit_session=FAKEFAKEFAKE1234\n",
            encoding="utf-8")
        with isolated_config(self.tmp):
            self.assertEqual(jobs.ingest_available(), (True, ""))
            self.assertIsNone(jobs.run_lock_busy())
            holder = self.hold("Backfill")
            ok, reason = jobs.ingest_available()
            self.assertFalse(ok)
            self.assertRegex(reason, rf"^Backfill is already running \(started \d\d:\d\d, process {holder.real_pid}\)\.$")
            self.assertEqual(jobs.run_lock_busy(), reason)
            self.kill_holder(holder)
            self.assertEqual(jobs.ingest_available(), (True, ""))


if __name__ == "__main__":
    unittest.main()
