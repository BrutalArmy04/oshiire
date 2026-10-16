"""Tests for app.py -- the desktop entry point.

No GUI is ever started here: pywebview's window/start calls are never invoked.
We test only the pure/headless pieces -- the close decision, stdio redirection,
jobs._child_python, and that review.launch_app exists. (The single-instance lock
itself moved to instance.py and is tested in tests/test_instance.py; here we
only check that main() bails out before stdio when the lock is already held.)

    python -m unittest discover -s tests
    python tests/test_app.py
"""
import contextlib
import io
import os
import subprocess
import sys
import tempfile
import textwrap
import types
import unittest
from pathlib import Path
from unittest import mock

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

import app
import jobs

# main() now chdirs to datahome.data_root(), merges settings into os.environ
# (config.load) and writes starter files. For every test in this module, point
# the data root at a throwaway folder via OSHIIRE_HOME and stub the config and
# starter steps, so no main() call reads the real .env or writes to the checkout.
_MODULE_PATCHES = []
_HOME = None


def setUpModule():
    global _HOME
    _HOME = Path(tempfile.mkdtemp(prefix="app-home-")).resolve()
    for patcher in (mock.patch.dict(os.environ, {"OSHIIRE_HOME": str(_HOME)}),
                    mock.patch.object(app.config, "load"),
                    mock.patch.object(app.config, "summary", return_value="config: (test)"),
                    mock.patch.object(app.config, "launch_environ", return_value=None),
                    mock.patch.object(app.starters, "ensure_starters", return_value=[])):
        patcher.start()
        _MODULE_PATCHES.append(patcher)


def tearDownModule():
    while _MODULE_PATCHES:
        _MODULE_PATCHES.pop().stop()
    import shutil
    shutil.rmtree(_HOME, ignore_errors=True)


class CloseDecisionTest(unittest.TestCase):
    """close_decision(running_name, cancellable). The caller passes
    cancellable = (name == "Backfill"); Backfill is the only cancellable job."""

    def test_no_job_closes(self):
        self.assertEqual(app.close_decision(None, False), "close")

    def test_backfill_asks_to_stop(self):
        self.assertEqual(app.close_decision("Backfill", True), "ask_stop")

    def test_non_cancellable_jobs_ask_to_wait(self):
        for name in ("Ingest", "Archive apply", "Archive dry run"):
            self.assertEqual(app.close_decision(name, False), "ask_wait", name)


class SetupStdioTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="app-stdio-"))
        self._out, self._err = sys.stdout, sys.stderr

    def tearDown(self):
        # Restore the real streams no matter what, closing any file we opened.
        opened = sys.stdout
        sys.stdout, sys.stderr = self._out, self._err
        if opened not in (self._out, self._err):
            try:
                opened.close()
            except Exception:
                pass

    def test_redirects_to_log_and_rotates_when_no_console(self):
        log = self.tmp / "oshiire.log"
        prev = self.tmp / "oshiire.prev.log"
        log.write_text("previous run\n", encoding="utf-8")  # must rotate to .prev

        sys.stdout = None  # simulate pythonw (no console)
        sys.stderr = None
        app._setup_stdio(log_dir=self.tmp)

        # Both streams point at the same open log file now.
        self.assertIsNotNone(sys.stdout)
        self.assertIs(sys.stdout, sys.stderr)
        print("hello from the log")
        sys.stdout.flush()

        self.assertIn("previous run", prev.read_text(encoding="utf-8"))
        self.assertIn("hello from the log", log.read_text(encoding="utf-8"))

    def test_leaves_real_console_untouched(self):
        # sys.stdout/sys.stderr are the real ones here (restored in setUp order).
        app._setup_stdio(log_dir=self.tmp)
        self.assertIs(sys.stdout, self._out)
        self.assertIs(sys.stderr, self._err)
        self.assertFalse((self.tmp / "oshiire.log").exists())


class ChildPythonTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="app-pyexe-"))

    def test_pythonw_with_sibling_returns_sibling(self):
        pythonw = self.tmp / "pythonw.exe"
        python = self.tmp / "python.exe"
        pythonw.write_text("", encoding="utf-8")
        python.write_text("", encoding="utf-8")
        self.assertEqual(jobs._child_python(str(pythonw)), str(python))

    def test_pythonw_without_sibling_unchanged(self):
        pythonw = self.tmp / "pythonw.exe"
        pythonw.write_text("", encoding="utf-8")
        self.assertEqual(jobs._child_python(str(pythonw)), str(pythonw))

    def test_plain_interpreter_unchanged(self):
        self.assertEqual(jobs._child_python("python3"), "python3")


class SecondInstanceStdioTest(unittest.TestCase):
    """A second launch must bail out BEFORE _setup_stdio, so it never rotates or
    truncates the log the first instance holds open -- in EITHER mode."""

    def test_native_second_instance_never_touches_stdio(self):
        prev_cwd = os.getcwd()  # main() chdirs; restore it afterwards
        try:
            with mock.patch.object(app, "acquire_single_instance", return_value=None), \
                 mock.patch.object(app.settings, "window_mode", return_value="native"), \
                 mock.patch.object(app, "_setup_stdio") as setup_stdio, \
                 mock.patch.object(app, "_handle_already_running", return_value=None) as handle, \
                 mock.patch.object(app.config, "load") as load, \
                 mock.patch.object(app.starters, "ensure_starters") as ensure, \
                 mock.patch("sys.exit", side_effect=SystemExit):
                with self.assertRaises(SystemExit):
                    app.main()
            setup_stdio.assert_not_called()
            handle.assert_called_once_with("native")
            # Even a second launch runs in the DATA ROOT (OSHIIRE_HOME here), not
            # the app folder, with settings loaded -- but it never writes starters
            # (it doesn't hold the lock).
            self.assertEqual(Path(os.getcwd()).resolve(), _HOME)
            load.assert_called_once_with()
            ensure.assert_not_called()
        finally:
            os.chdir(prev_cwd)

    def test_browser_second_instance_opens_port_and_never_touches_stdio(self):
        prev_cwd = os.getcwd()
        try:
            with mock.patch.object(app, "acquire_single_instance", return_value=None), \
                 mock.patch.object(app.settings, "window_mode", return_value="browser"), \
                 mock.patch.object(app.settings, "theme", return_value="system"), \
                 mock.patch.object(app, "read_lock_info", return_value=(7860, None)), \
                 mock.patch.object(app, "_wait_for_recorded_port", return_value=7860), \
                 mock.patch("webbrowser.open") as wopen, \
                 mock.patch.object(app, "_setup_stdio") as setup_stdio, \
                 mock.patch.object(app, "_show_already_running") as show, \
                 mock.patch("sys.exit", side_effect=SystemExit):
                with self.assertRaises(SystemExit):
                    app.main()
            # Browser fast path: the recorded port is open as a tab directly, and
            # the Open/Stop window is never shown.
            wopen.assert_called_once_with("http://127.0.0.1:7860/")
            setup_stdio.assert_not_called()
            show.assert_not_called()
            self.assertEqual(Path(os.getcwd()).resolve(), _HOME)  # the data root
        finally:
            os.chdir(prev_cwd)


class ModeDispatchTest(unittest.TestCase):
    """main() dispatches to the native window or the browser by the saved mode,
    and _run_native falls back to the browser when pywebview can't start. No real
    server or window is ever created -- the run functions are mocked."""

    def setUp(self):
        self._prev_cwd = os.getcwd()
        self._prev_lock = app._lock

    def tearDown(self):
        os.chdir(self._prev_cwd)
        app._lock = self._prev_lock

    def test_themed_url_by_theme(self):
        self.assertEqual(app._themed_url(7861, theme="system"),
                         "http://127.0.0.1:7861/")
        self.assertEqual(app._themed_url(7861, theme="light"),
                         "http://127.0.0.1:7861/?__theme=light")
        self.assertEqual(app._themed_url(7861, theme="dark"),
                         "http://127.0.0.1:7861/?__theme=dark")

    def test_first_launch_browser_mode_runs_browser(self):
        handle = object()
        with mock.patch.object(app, "acquire_single_instance", return_value=handle), \
             mock.patch.object(app.settings, "window_mode", return_value="browser"), \
             mock.patch.object(app, "_setup_stdio"), \
             mock.patch.object(app, "_check_numpy", return_value=None), \
             mock.patch.object(app, "_run_browser") as run_browser, \
             mock.patch.object(app, "_run_native") as run_native:
            app.main()
        run_browser.assert_called_once_with(handle)
        run_native.assert_not_called()

    def test_first_launch_native_mode_runs_native(self):
        handle = object()
        with mock.patch.object(app, "acquire_single_instance", return_value=handle), \
             mock.patch.object(app.settings, "window_mode", return_value="native"), \
             mock.patch.object(app, "_setup_stdio"), \
             mock.patch.object(app, "_check_numpy", return_value=None), \
             mock.patch.object(app, "_run_browser") as run_browser, \
             mock.patch.object(app, "_run_native") as run_native:
            app.main()
        run_native.assert_called_once_with(handle)
        run_browser.assert_not_called()

    def test_native_falls_back_to_browser_when_pywebview_missing(self):
        # `import webview` raises ImportError when the name maps to None.
        with mock.patch.dict(sys.modules, {"webview": None}), \
             mock.patch.object(app, "_run_browser") as run_browser:
            app._run_native(object())
        run_browser.assert_called_once()

    def test_native_falls_back_to_browser_when_start_raises(self):
        # MagicMock so window.events.closing += handler (a real pywebview event
        # supports +=) doesn't choke on a plain Mock.
        fake_webview = mock.MagicMock()
        fake_webview.start.side_effect = RuntimeError("no GUI backend")
        import contextlib, io
        with mock.patch.dict(sys.modules, {"webview": fake_webview}), \
             mock.patch.object(app, "_run_browser") as run_browser, \
             contextlib.redirect_stderr(io.StringIO()):
            app._run_native(object())
        run_browser.assert_called_once()


class StartupRaceTest(unittest.TestCase):
    """A fresh launch clears any stale port on acquiring the lock; a second
    launch in browser mode waits for the running instance to publish its port
    before opening a tab, and falls back to the 'already open' window if none
    appears in time."""

    def setUp(self):
        self._prev_cwd = os.getcwd()
        self._prev_lock = app._lock

    def tearDown(self):
        os.chdir(self._prev_cwd)
        app._lock = self._prev_lock

    def test_first_launch_clears_stale_port_after_acquiring_lock(self):
        handle = object()
        with mock.patch.object(app, "acquire_single_instance", return_value=handle), \
             mock.patch.object(app.settings, "window_mode", return_value="browser"), \
             mock.patch.object(app, "clear_recorded_port") as clear, \
             mock.patch.object(app, "_setup_stdio"), \
             mock.patch.object(app, "_check_numpy", return_value=None), \
             mock.patch.object(app, "_run_browser"), \
             mock.patch.object(app, "_run_native"):
            app.main()
        clear.assert_called_once()

    def test_wait_picks_up_a_port_written_mid_wait(self):
        with mock.patch.object(app, "read_recorded_port", side_effect=[None, None, 7861]), \
             mock.patch.object(app.time, "sleep") as sleep, \
             mock.patch.object(app.time, "monotonic", side_effect=[0.0, 0.5, 1.0, 1.5]):
            port = app._wait_for_recorded_port(timeout=30, interval=0.5)
        self.assertEqual(port, 7861)
        self.assertTrue(sleep.called)  # it genuinely waited

    def test_wait_times_out_and_returns_none(self):
        with mock.patch.object(app, "read_recorded_port", return_value=None), \
             mock.patch.object(app.time, "sleep"), \
             mock.patch.object(app.time, "monotonic", side_effect=[0.0, 5.0, 40.0]):
            self.assertIsNone(app._wait_for_recorded_port(timeout=30, interval=0.5))

    def test_second_launch_browser_waits_then_opens_tab(self):
        prev_cwd = os.getcwd()
        try:
            with mock.patch.object(app, "acquire_single_instance", return_value=None), \
                 mock.patch.object(app.settings, "window_mode", return_value="browser"), \
                 mock.patch.object(app.settings, "theme", return_value="system"), \
                 mock.patch.object(app, "read_lock_info", return_value=(7861, None)), \
                 mock.patch.object(app, "_wait_for_recorded_port", return_value=7861), \
                 mock.patch("webbrowser.open") as wopen, \
                 mock.patch.object(app, "_setup_stdio") as stdio, \
                 mock.patch("sys.exit", side_effect=SystemExit):
                with self.assertRaises(SystemExit):
                    app.main()
            wopen.assert_called_once_with("http://127.0.0.1:7861/")
            stdio.assert_not_called()
        finally:
            os.chdir(prev_cwd)

    def test_second_launch_browser_timeout_shows_window(self):
        prev_cwd = os.getcwd()
        try:
            with mock.patch.object(app, "acquire_single_instance", return_value=None), \
                 mock.patch.object(app.settings, "window_mode", return_value="browser"), \
                 mock.patch.object(app, "read_lock_info", return_value=(None, None)), \
                 mock.patch.object(app, "_wait_for_recorded_port", return_value=None), \
                 mock.patch("webbrowser.open") as wopen, \
                 mock.patch.object(app, "_setup_stdio") as stdio, \
                 mock.patch.object(app, "_show_already_running",
                                   return_value=("close", None)) as show, \
                 mock.patch("sys.exit", side_effect=SystemExit):
                with self.assertRaises(SystemExit):
                    app.main()
            # No recorded port -> no silent tab; the Open/Stop window is shown.
            show.assert_called_once()
            wopen.assert_not_called()
            stdio.assert_not_called()  # the poll+window path never touches stdio
        finally:
            os.chdir(prev_cwd)

    def test_second_launch_never_continues_in_process(self):
        """A second launch always exits after _handle_already_running (which, for
        'Stop it and start fresh', has already spawned a fresh detached app.py).
        main() never falls through to _run_native/_run_browser or touches stdio
        in THIS process."""
        prev_cwd, prev_lock = os.getcwd(), app._lock
        try:
            with mock.patch.object(app, "acquire_single_instance", return_value=None), \
                 mock.patch.object(app.settings, "window_mode", return_value="native"), \
                 mock.patch.object(app, "_handle_already_running") as handle, \
                 mock.patch.object(app, "_setup_stdio") as stdio, \
                 mock.patch.object(app, "_run_native") as run_native, \
                 mock.patch.object(app, "_run_browser") as run_browser, \
                 mock.patch("sys.exit", side_effect=SystemExit):
                with self.assertRaises(SystemExit):
                    app.main()
            handle.assert_called_once_with("native")
            run_native.assert_not_called()
            run_browser.assert_not_called()
            stdio.assert_not_called()
        finally:
            os.chdir(prev_cwd)
            app._lock = prev_lock

    def test_handle_already_running_stop_action_triggers_restart(self):
        """When the window returns ('stop', …), _handle_already_running calls
        _stop_and_restart with the lock's recorded PID and opens no tab."""
        with mock.patch.object(app, "read_lock_info", return_value=(None, 4321)), \
             mock.patch.object(app, "_wait_for_recorded_port", return_value=None), \
             mock.patch.object(app, "_show_already_running", return_value=("stop", None)), \
             mock.patch.object(app, "_stop_and_restart") as restart, \
             mock.patch("webbrowser.open") as wopen:
            app._handle_already_running("native")
        restart.assert_called_once_with(4321)
        wopen.assert_not_called()


class ClosingHandlerTest(unittest.TestCase):
    """The closing handler fails CLOSED: if anything inside it raises, it keeps
    the window open while a job runs, but still allows the close when idle."""

    def setUp(self):
        self._prev_review = app._review
        self._prev_pending = app._closing_pending

    def tearDown(self):
        app._review = self._prev_review
        app._closing_pending = self._prev_pending

    def test_closing_handler_fails_closed_when_busy(self):
        class _Jobs:
            name = "Archive apply"
            @classmethod
            def running(cls):
                return cls.name

        class _Review:
            jobs = _Jobs

        class _Win:
            def create_confirmation_dialog(self, *a, **k):
                raise RuntimeError("dialog blew up")
            def destroy(self):
                pass

        app._review = _Review
        app._closing_pending = False
        handler = app._on_closing(_Win())

        # Busy + the dialog raises -> fail closed: do NOT allow the close. The
        # handler logs the traceback (expected here) -- swallow it for a clean
        # test run.
        import contextlib, io
        with contextlib.redirect_stderr(io.StringIO()):
            self.assertFalse(handler())

        # Idle -> the close is allowed (the dialog is never even consulted).
        _Jobs.name = None
        self.assertTrue(handler())


class WaiterOnceTest(unittest.TestCase):
    def setUp(self):
        self._prev_review = app._review
        self._prev_pending = app._closing_pending

    def tearDown(self):
        app._review = self._prev_review
        app._closing_pending = self._prev_pending

    def test_waiter_starts_once(self):
        app._closing_pending = False
        with mock.patch.object(app.threading, "Thread") as Thread:
            window = mock.Mock()
            app._start_waiter(window)
            app._start_waiter(window)  # second call must be a no-op
            self.assertEqual(Thread.call_count, 1)
            Thread.return_value.start.assert_called_once()


class LaunchAppExistsTest(unittest.TestCase):
    """review must expose a callable launch_app (app.py's _boot calls it). We
    probe it in a SUBPROCESS: importing review binds its manifest/layout/queue
    from the cwd at import time, so importing it here would poison the module
    cache for sibling tests that import review inside their own sandbox. We
    never call launch_app -- only check that it is callable."""

    def test_review_exposes_callable_launch_app(self):
        script = textwrap.dedent(f"""
            import sys
            sys.path.insert(0, {str(REPO)!r})
            import review
            print("CALLABLE" if callable(review.launch_app) else "NO")
        """)
        out = subprocess.run(
            [sys.executable, "-c", script],
            capture_output=True, text=True, timeout=300,
        )
        self.assertIn("CALLABLE", out.stdout, out.stderr)


class CmdlineRunsOurAppTest(unittest.TestCase):
    """_cmdline_runs_our_app -- the whole safety test for 'Stop it and start
    fresh'. A PID is terminated only when this returns True, so it must match
    this repo's app.py exactly and nothing else."""

    def test_absolute_app_py_matches(self):
        self.assertTrue(app._cmdline_runs_our_app([sys.executable, str(app.APP_PY)]))

    def test_absolute_other_app_py_does_not_match(self):
        other = Path(tempfile.mkdtemp(prefix="other-")) / "app.py"
        other.write_text("", encoding="utf-8")
        self.assertFalse(app._cmdline_runs_our_app(["pythonw.exe", str(other)]))

    def test_relative_app_py_with_repo_cwd_matches(self):
        self.assertTrue(app._cmdline_runs_our_app(["pythonw.exe", "app.py"],
                                                  cwd=str(app.REPO_ROOT)))

    def test_relative_app_py_with_wrong_cwd_does_not_match(self):
        elsewhere = tempfile.mkdtemp(prefix="elsewhere-")
        self.assertFalse(app._cmdline_runs_our_app(["pythonw.exe", "app.py"],
                                                   cwd=elsewhere))

    def test_no_app_py_token_does_not_match(self):
        self.assertFalse(app._cmdline_runs_our_app([sys.executable, "review.py"]))

    def test_empty_cmdline_does_not_match(self):
        self.assertFalse(app._cmdline_runs_our_app([]))
        self.assertFalse(app._cmdline_runs_our_app(None))


class FindHolderPidTest(unittest.TestCase):
    """_find_holder_pid(lock_pid): prefer the recorded PID once verified, else
    scan. This is where 'the lock had no PID' is handled."""

    def test_recorded_pid_used_when_it_verifies(self):
        with mock.patch.object(app, "_pid_runs_our_app", return_value=True) as verify, \
             mock.patch.object(app, "_scan_for_holder_pid") as scan:
            self.assertEqual(app._find_holder_pid(1234), 1234)
        verify.assert_called_once_with(1234)
        scan.assert_not_called()

    def test_recorded_pid_rejected_then_scans(self):
        with mock.patch.object(app, "_pid_runs_our_app", return_value=False), \
             mock.patch.object(app, "_scan_for_holder_pid", return_value=999):
            self.assertEqual(app._find_holder_pid(1234), 999)

    def test_no_recorded_pid_scans(self):
        # Old lock format (pid None): never verify, go straight to the scan.
        with mock.patch.object(app, "_pid_runs_our_app") as verify, \
             mock.patch.object(app, "_scan_for_holder_pid", return_value=777):
            self.assertEqual(app._find_holder_pid(None), 777)
        verify.assert_not_called()


class PidRunsOurAppTest(unittest.TestCase):
    def test_true_when_cmdline_matches(self):
        with mock.patch.object(app, "_pid_cmdline",
                               return_value=(["pythonw.exe", str(app.APP_PY)], None)):
            self.assertTrue(app._pid_runs_our_app(4242))

    def test_false_when_cmdline_unreadable(self):
        with mock.patch.object(app, "_pid_cmdline", return_value=(None, None)):
            self.assertFalse(app._pid_runs_our_app(4242))

    def test_false_when_cmdline_is_a_different_app(self):
        with mock.patch.object(app, "_pid_cmdline",
                               return_value=(["pythonw.exe", "review.py"], None)):
            self.assertFalse(app._pid_runs_our_app(4242))


class QuotedCmdlineTest(unittest.TestCase):
    """A spaced app.py path comes back quoted from the wmic/PowerShell parsers
    (shlex posix=False keeps the quotes). The quotes must not defeat the match --
    and this repo's own path contains a space."""

    def test_double_quoted_absolute_app_py_matches(self):
        self.assertTrue(app._cmdline_runs_our_app(["pythonw.exe", f'"{app.APP_PY}"']))

    def test_single_quoted_absolute_app_py_matches(self):
        self.assertTrue(app._cmdline_runs_our_app(["pythonw.exe", f"'{app.APP_PY}'"]))


class PowerShellFallbackTest(unittest.TestCase):
    """When psutil is absent on Windows, the CommandLine comes from PowerShell's
    CIM (wmic is gone on current Windows 11); wmic is only the last resort."""

    def test_powershell_cmdline_parses_quoted_commandline(self):
        with mock.patch.object(app, "_run_capture",
                               return_value=f'pythonw.exe "{app.APP_PY}"\n'):
            cmdline = app._powershell_cmdline(1234)
        self.assertTrue(app._cmdline_runs_our_app(cmdline))

    def test_powershell_cmdline_none_for_empty_output(self):
        with mock.patch.object(app, "_run_capture", return_value="\n"):
            self.assertIsNone(app._powershell_cmdline(1234))
        with mock.patch.object(app, "_run_capture", return_value=None):
            self.assertIsNone(app._powershell_cmdline(1234))

    def test_powershell_scan_returns_matching_pid(self):
        out = (f'1000|pythonw.exe "{app.REPO_ROOT / "review.py"}"\n'
               f'999|pythonw.exe "{app.APP_PY}"\n')
        with mock.patch.object(app, "_run_capture", return_value=out):
            self.assertEqual(app._powershell_scan(), 999)

    def test_powershell_scan_none_when_nothing_matches(self):
        with mock.patch.object(app, "_run_capture",
                               return_value='1000|pythonw.exe "other.py"\n'):
            self.assertIsNone(app._powershell_scan())

    def test_pid_cmdline_prefers_powershell_over_wmic(self):
        with mock.patch.object(app, "_psutil", return_value=None), \
             mock.patch.object(app.sys, "platform", "win32"), \
             mock.patch.object(app, "_powershell_cmdline",
                               return_value=["pythonw.exe", str(app.APP_PY)]), \
             mock.patch.object(app, "_wmic_cmdline",
                               side_effect=AssertionError("wmic must not be tried first")):
            self.assertEqual(app._pid_cmdline(1234),
                             (["pythonw.exe", str(app.APP_PY)], None))

    def test_pid_cmdline_falls_back_to_wmic_when_powershell_empty(self):
        with mock.patch.object(app, "_psutil", return_value=None), \
             mock.patch.object(app.sys, "platform", "win32"), \
             mock.patch.object(app, "_powershell_cmdline", return_value=None), \
             mock.patch.object(app, "_wmic_cmdline", return_value=["pythonw.exe", "app.py"]):
            self.assertEqual(app._pid_cmdline(1234), (["pythonw.exe", "app.py"], None))

    def test_scan_prefers_powershell_then_wmic(self):
        with mock.patch.object(app, "_psutil", return_value=None), \
             mock.patch.object(app.sys, "platform", "win32"), \
             mock.patch.object(app, "_powershell_scan", return_value=555), \
             mock.patch.object(app, "_wmic_scan",
                               side_effect=AssertionError("wmic must not be tried first")):
            self.assertEqual(app._scan_for_holder_pid(), 555)

    def test_scan_falls_back_to_wmic_when_powershell_none(self):
        with mock.patch.object(app, "_psutil", return_value=None), \
             mock.patch.object(app.sys, "platform", "win32"), \
             mock.patch.object(app, "_powershell_scan", return_value=None), \
             mock.patch.object(app, "_wmic_scan", return_value=666):
            self.assertEqual(app._scan_for_holder_pid(), 666)


class DiscoverPortTest(unittest.TestCase):
    def test_known_port_trusted_without_probing(self):
        with mock.patch.object(app, "_page_is_oshiire",
                               side_effect=AssertionError("must not probe")):
            self.assertEqual(app._discover_oshiire_port(7860), 7860)

    def test_probes_and_returns_first_oshiire_port(self):
        hit = list(app._PROBE_PORTS)[2]
        with mock.patch.object(app, "_page_is_oshiire", side_effect=lambda p: p == hit):
            self.assertEqual(app._discover_oshiire_port(None), hit)

    def test_returns_none_when_nothing_answers_as_oshiire(self):
        with mock.patch.object(app, "_page_is_oshiire", return_value=False):
            self.assertIsNone(app._discover_oshiire_port(None))


class PageIsOshiireTest(unittest.TestCase):
    """The responder must really be Oshiire -- judged by the page <title>, so a
    different local server on 7860 is never mistaken for ours."""

    def _resp(self, text):
        r = mock.Mock()
        r.text = text
        return r

    def test_true_for_oshiire_title(self):
        with mock.patch("requests.get", return_value=self._resp(
                "<html><head><title>Oshiire</title></head></html>")):
            self.assertTrue(app._page_is_oshiire(7860))

    def test_false_for_other_title(self):
        with mock.patch("requests.get", return_value=self._resp(
                "<html><head><title>Some Other App</title></head></html>")):
            self.assertFalse(app._page_is_oshiire(7860))

    def test_false_on_connection_error(self):
        with mock.patch("requests.get", side_effect=OSError("refused")):
            self.assertFalse(app._page_is_oshiire(7860))


class WaitForLockFreeTest(unittest.TestCase):
    def test_returns_handle_once_the_lock_frees(self):
        handle = object()
        with mock.patch.object(app, "acquire_single_instance",
                               side_effect=[None, handle]), \
             mock.patch.object(app.time, "sleep"), \
             mock.patch.object(app.time, "monotonic", side_effect=[0.0, 0.2, 0.4]):
            self.assertIs(app._wait_for_lock_free(timeout=5, interval=0.2), handle)

    def test_times_out_returns_none(self):
        with mock.patch.object(app, "acquire_single_instance", return_value=None), \
             mock.patch.object(app.time, "sleep"), \
             mock.patch.object(app.time, "monotonic", side_effect=[0.0, 10.0, 20.0]):
            self.assertIsNone(app._wait_for_lock_free(timeout=5, interval=0.2))


class StopAndRestartTest(unittest.TestCase):
    """'Stop it and start fresh': verify-then-terminate, release the lock, spawn a
    fresh detached app.py, and let this process exit. Nothing is killed unless its
    command line verifies."""

    # The no-op/refusal paths print a one-line reason to stdout; swallow it so
    # the suite output stays clean (these prints are expected here).
    def _quiet(self):
        return contextlib.redirect_stdout(io.StringIO())

    def test_no_pid_found_spawns_nothing(self):
        with mock.patch.object(app, "_find_holder_pid", return_value=None), \
             mock.patch.object(app, "_terminate_pid") as kill, \
             mock.patch.object(app, "_spawn_fresh_app") as spawn, self._quiet():
            self.assertFalse(app._stop_and_restart(None))
        kill.assert_not_called()
        spawn.assert_not_called()

    def test_refuses_and_spawns_nothing_when_final_verify_fails(self):
        # _find_holder_pid returned something, but the final guard rejects it:
        # never terminate a process that doesn't verify.
        with mock.patch.object(app, "_find_holder_pid", return_value=1234), \
             mock.patch.object(app, "_pid_runs_our_app", return_value=False), \
             mock.patch.object(app, "_terminate_pid") as kill, \
             mock.patch.object(app, "_spawn_fresh_app") as spawn, self._quiet():
            self.assertFalse(app._stop_and_restart(1234))
        kill.assert_not_called()
        spawn.assert_not_called()

    def test_happy_path_releases_lock_then_spawns(self):
        handle = mock.Mock()  # the lock _wait_for_lock_free took to prove it free
        with mock.patch.object(app, "_find_holder_pid", return_value=1234), \
             mock.patch.object(app, "_pid_runs_our_app", return_value=True), \
             mock.patch.object(app, "_terminate_pid", return_value=True) as kill, \
             mock.patch.object(app, "_wait_for_lock_free", return_value=handle), \
             mock.patch.object(app, "_spawn_fresh_app") as spawn:
            self.assertTrue(app._stop_and_restart(1234))
        kill.assert_called_once_with(1234)
        handle.close.assert_called_once()  # lock freed BEFORE the fresh spawn
        spawn.assert_called_once()

    def test_lock_never_frees_spawns_nothing(self):
        with mock.patch.object(app, "_find_holder_pid", return_value=1234), \
             mock.patch.object(app, "_pid_runs_our_app", return_value=True), \
             mock.patch.object(app, "_terminate_pid", return_value=True), \
             mock.patch.object(app, "_wait_for_lock_free", return_value=None), \
             mock.patch.object(app, "_spawn_fresh_app") as spawn, self._quiet():
            self.assertFalse(app._stop_and_restart(1234))
        spawn.assert_not_called()


class SpawnFreshAppTest(unittest.TestCase):
    def test_spawns_detached_app_py_in_data_root_cwd(self):
        # The running instance's cwd IS the data root; the fresh one starts there
        # with the absolute app.py, and with the pre-config environment so it
        # re-reads .env/config.env itself.
        pre_config_env = {"ONLY": "pre-config"}
        prev_cwd = os.getcwd()
        data_root = tempfile.mkdtemp(prefix="app-spawn-root-")
        try:
            os.chdir(data_root)
            with mock.patch.object(app.subprocess, "Popen") as popen, \
                 mock.patch.object(app.config, "launch_environ", return_value=pre_config_env):
                app._spawn_fresh_app()
        finally:
            os.chdir(prev_cwd)
        popen.assert_called_once()
        args, kwargs = popen.call_args
        self.assertEqual(args[0], [app._relaunch_interpreter(), str(app.APP_PY)])
        self.assertTrue(Path(args[0][1]).is_absolute())
        self.assertEqual(Path(kwargs["cwd"]).resolve(), Path(data_root).resolve())
        self.assertIs(kwargs["env"], pre_config_env)
        # Detached, no console: creationflags on Windows, start_new_session on POSIX.
        if sys.platform == "win32":
            self.assertIn("creationflags", kwargs)
            self.assertTrue(kwargs["creationflags"]
                            & app.subprocess.DETACHED_PROCESS)
        else:
            self.assertTrue(kwargs.get("start_new_session"))

    def test_prefers_pythonw_on_windows(self):
        # python.exe with a pythonw.exe sibling -> relaunch with pythonw (no
        # console). Exercised cross-platform by faking the platform + sibling.
        tmp = Path(tempfile.mkdtemp(prefix="relaunch-"))
        (tmp / "python.exe").write_text("", encoding="utf-8")
        (tmp / "pythonw.exe").write_text("", encoding="utf-8")
        with mock.patch.object(app.sys, "platform", "win32"), \
             mock.patch.object(app.sys, "executable", str(tmp / "python.exe")):
            self.assertEqual(app._relaunch_interpreter(), str(tmp / "pythonw.exe"))


class NumpyFaultTest(unittest.TestCase):
    """_numpy_fault -- the pure version/build decision. A too-old major version or
    the experimental MinGW build is a fault; an unparseable version is not."""

    def test_old_version_is_a_fault(self):
        reason = app._numpy_fault("1.26.4", mingw=False)
        self.assertIsNotNone(reason)
        self.assertIn("1.26.4", reason)

    def test_modern_version_is_fine(self):
        self.assertIsNone(app._numpy_fault("2.5.1", mingw=False))

    def test_mingw_build_is_a_fault_even_when_version_parses_ok(self):
        reason = app._numpy_fault("2.0.0", mingw=True)
        self.assertIsNotNone(reason)
        self.assertIn("MinGW", reason)

    def test_unparseable_version_not_mistaken_for_old(self):
        self.assertIsNone(app._numpy_fault("", mingw=False))
        self.assertIsNone(app._numpy_fault("garbage", mingw=False))

    def test_parse_major(self):
        self.assertEqual(app._parse_major("2.5.1"), 2)
        self.assertEqual(app._parse_major("1.26.4"), 1)
        self.assertIsNone(app._parse_major("garbage"))


class NumpyMingwDetectTest(unittest.TestCase):
    """_numpy_looks_mingw -- conservative detection of the experimental MinGW
    build via the import warning (primary) or the build config (secondary). It
    must never flag an ordinary numpy."""

    def test_import_warning_flags_mingw(self):
        warned = types.SimpleNamespace(
            message="Numpy built with MINGW-W64 on Windows 64 bits is experimental")
        self.assertTrue(app._numpy_looks_mingw(object(), [warned]))

    def test_no_warning_no_config_is_not_mingw(self):
        module = mock.Mock()
        module.show_config.side_effect = TypeError("no mode kwarg")
        self.assertFalse(app._numpy_looks_mingw(module, []))

    def test_config_naming_mingw_flags(self):
        module = mock.Mock()
        module.show_config.return_value = {
            "Compilers": {"c": {"commands": "x86_64-w64-mingw32-gcc"}}}
        self.assertTrue(app._numpy_looks_mingw(module, []))

    def test_ordinary_config_is_not_flagged(self):
        module = mock.Mock()
        module.show_config.return_value = {
            "Compilers": {"c": {"name": "msvc", "commands": "cl.exe"}}}
        self.assertFalse(app._numpy_looks_mingw(module, []))


class CheckNumpyTest(unittest.TestCase):
    """_check_numpy imports numpy fresh and classifies it. A fake numpy module is
    injected so the result never depends on what's actually installed."""

    def _fake_numpy(self, version="2.5.1", config=None):
        fake = types.ModuleType("numpy")
        fake.__version__ = version
        fake.show_config = lambda mode=None: (config if config is not None else {})
        return fake

    def test_good_numpy_returns_none(self):
        with mock.patch.dict(sys.modules, {"numpy": self._fake_numpy("2.5.1")}):
            self.assertIsNone(app._check_numpy())

    def test_old_numpy_returns_reason(self):
        with mock.patch.dict(sys.modules, {"numpy": self._fake_numpy("1.26.4")}):
            reason = app._check_numpy()
        self.assertIsNotNone(reason)
        self.assertIn("1.26.4", reason)

    def test_mingw_config_returns_reason(self):
        fake = self._fake_numpy(
            "2.0.0", config={"Compilers": {"c": {"commands": "x86_64-w64-mingw32-gcc"}}})
        with mock.patch.dict(sys.modules, {"numpy": fake}):
            reason = app._check_numpy()
        self.assertIsNotNone(reason)
        self.assertIn("MinGW", reason)

    def test_import_failure_returns_reason(self):
        # A None entry in sys.modules makes `import numpy` raise ImportError.
        with mock.patch.dict(sys.modules, {"numpy": None}):
            reason = app._check_numpy()
        self.assertIsNotNone(reason)
        self.assertIn("numpy", reason.lower())


class MainNumpyGateTest(unittest.TestCase):
    """main() runs the numpy check after taking the lock and BEFORE dispatching
    to a run function: a bad numpy shows the error window and exits non-zero,
    never reaching _run_browser/_run_native (which would import review)."""

    def test_bad_numpy_shows_window_and_exits_before_dispatch(self):
        prev_cwd = os.getcwd()
        try:
            with mock.patch.object(app, "acquire_single_instance", return_value=object()), \
                 mock.patch.object(app.settings, "window_mode", return_value="browser"), \
                 mock.patch.object(app, "clear_recorded_port"), \
                 mock.patch.object(app, "_setup_stdio"), \
                 mock.patch.object(app, "_check_numpy",
                                   return_value="numpy 1.26.4 is too old"), \
                 mock.patch.object(app, "_show_error_window") as show, \
                 mock.patch.object(app, "_run_browser") as run_browser, \
                 mock.patch.object(app, "_run_native") as run_native, \
                 contextlib.redirect_stdout(io.StringIO()), \
                 mock.patch("sys.exit", side_effect=SystemExit):
                with self.assertRaises(SystemExit):
                    app.main()
            show.assert_called_once()
            # The fix command is passed so the window can tell the user what to run.
            self.assertEqual(show.call_args.kwargs.get("fix"), app._NUMPY_FIX)
            run_browser.assert_not_called()
            run_native.assert_not_called()
        finally:
            os.chdir(prev_cwd)


class RunGuardedTest(unittest.TestCase):
    """_run_guarded -- the never-fail-silently wrapper: a startup exception is
    surfaced in the error window and the process exits non-zero, while a clean
    SystemExit passes straight through."""

    def test_startup_exception_shows_window_and_exits_nonzero(self):
        with mock.patch.object(app, "main", side_effect=RuntimeError("boom")), \
             mock.patch.object(app, "_setup_stdio"), \
             mock.patch.object(app, "_show_error_window") as show, \
             contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit) as cm:
                app._run_guarded()
        show.assert_called_once()
        self.assertNotEqual(cm.exception.code, 0)
        # The reason carries the exception type and message.
        self.assertIn("boom", show.call_args.args[0])

    def test_clean_systemexit_passes_through_without_a_window(self):
        with mock.patch.object(app, "main", side_effect=SystemExit(0)), \
             mock.patch.object(app, "_show_error_window") as show:
            with self.assertRaises(SystemExit) as cm:
                app._run_guarded()
        show.assert_not_called()
        self.assertEqual(cm.exception.code, 0)


class ShowErrorWindowTest(unittest.TestCase):
    """_show_error_window must never raise -- if tkinter is unavailable it falls
    back to printing the message (which includes the reason, fix and log path)."""

    def test_falls_back_to_print_without_tkinter(self):
        buf = io.StringIO()
        with mock.patch.dict(sys.modules, {"tkinter": None}), \
             contextlib.redirect_stdout(buf):
            app._show_error_window("numpy is too old", fix=app._NUMPY_FIX)
        printed = buf.getvalue()
        self.assertIn("numpy is too old", printed)
        self.assertIn(app._NUMPY_FIX, printed)
        self.assertIn("oshiire.log", printed)


class MainDataRootTest(unittest.TestCase):
    """main() enters the DATA ROOT before anything else: it creates it, chdirs
    there, loads settings, and only then reads the launch mode; after taking the
    lock it writes starters BEFORE the numpy check and the run function (which
    imports review, whose import needs layout.json)."""

    def setUp(self):
        self._prev_cwd = os.getcwd()
        self._prev_lock = app._lock

    def tearDown(self):
        os.chdir(self._prev_cwd)
        app._lock = self._prev_lock

    def test_order_creates_root_enters_it_loads_config_then_starters_before_review(self):
        root = Path(tempfile.mkdtemp(prefix="app-root-")) / "not" / "there" / "yet"
        seen = mock.Mock()
        seen.load.side_effect = lambda: seen.cwd_at_load(Path(os.getcwd()).resolve())
        seen.window_mode.return_value = "browser"
        seen.acquire.return_value = object()
        seen.ensure_starters.return_value = []
        seen.check_numpy.return_value = None
        with mock.patch.dict(os.environ, {"OSHIIRE_HOME": str(root)}), \
             mock.patch.object(app.config, "load", seen.load), \
             mock.patch.object(app.settings, "window_mode", seen.window_mode), \
             mock.patch.object(app, "acquire_single_instance", seen.acquire), \
             mock.patch.object(app, "clear_recorded_port"), \
             mock.patch.object(app, "_setup_stdio"), \
             mock.patch.object(app.starters, "ensure_starters", seen.ensure_starters), \
             mock.patch.object(app, "_check_numpy", seen.check_numpy), \
             mock.patch.object(app, "_run_browser", seen.run_browser), \
             contextlib.redirect_stdout(io.StringIO()):
            app.main()
        self.assertTrue(root.is_dir())  # created, parents included
        self.assertEqual(Path(os.getcwd()).resolve(), root.resolve())
        names = [c[0] for c in seen.mock_calls if c[0] in (
            "load", "cwd_at_load", "window_mode", "acquire", "ensure_starters",
            "check_numpy", "run_browser")]
        self.assertEqual(names, ["load", "cwd_at_load", "window_mode", "acquire",
                                 "ensure_starters", "check_numpy", "run_browser"])
        # config.load ran with the data root already the cwd.
        seen.cwd_at_load.assert_called_once_with(root.resolve())

    def test_checkout_numpy_fix_is_the_venv_command(self):
        # This repo is a git checkout (no release marker).
        self.assertFalse(app.datahome.is_release())
        self.assertIn(".venv-win", app._NUMPY_FIX)
        self.assertTrue(app._NUMPY_FIX_IS_COMMAND)

    def test_release_numpy_fix_says_download_again(self):
        import importlib
        try:
            with mock.patch.object(app.datahome, "is_release", return_value=True):
                importlib.reload(app)
                fix, is_command = app._NUMPY_FIX, app._NUMPY_FIX_IS_COMMAND
        finally:
            importlib.reload(app)  # back to the checkout's values
        self.assertIn("Download Oshiire again", fix)
        self.assertNotIn("pip", fix)
        self.assertFalse(is_command)

    def test_error_window_words_a_non_command_fix_plainly(self):
        out = io.StringIO()
        with mock.patch.dict(sys.modules, {"tkinter": None}), contextlib.redirect_stdout(out):
            app._show_error_window("numpy is broken", fix="Download Oshiire again.",
                                   fix_is_command=False)
        printed = out.getvalue()
        self.assertIn("To fix it: Download Oshiire again.", printed)
        self.assertNotIn("run this from the Oshiire folder", printed)
        # The log path is shown absolute -- in a release it is not next to the app.
        log_line = [l for l in printed.splitlines() if l.startswith("Full log: ")][0]
        self.assertTrue(Path(log_line[len("Full log: "):]).is_absolute())


if __name__ == "__main__":
    unittest.main(verbosity=2)
