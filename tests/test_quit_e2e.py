"""End-to-end Quit test: a REAL app.py browser-mode process, driven by a real
browser, is stopped with the Home 'Quit Oshiire' button.

It confirms the three things the UI promises:
  (a) the process exits on its own shortly after Quit,
  (b) the single-instance lock is released (a fresh acquire succeeds),
  (c) data/oshiire.log is flushed and closed (readable, non-empty).

Everything runs in a throwaway copy of the project built only from COMMITTED
files (code + font assets + the *.example* configs renamed into place) plus a
minimal .env the test writes itself -- the real .env, manifest, data/, staging/
and archive are never touched. It is launched with pythonw.exe so there is no
console and stdout is redirected to the sandbox's data/oshiire.log (exactly as a
real double-click does). No REAL browser opens: the BROWSER env points
webbrowser at a no-op command, and the page is driven with Playwright, preferring
Microsoft Edge (channel="msedge") and falling back to the bundled Chromium.

This test is OPT-IN and Windows-only. It runs only when OSHIIRE_E2E=1 is set, and
skips cleanly otherwise, off Windows, or when Playwright / pythonw isn't present.

    set OSHIIRE_E2E=1 && python -m unittest tests.test_quit_e2e
"""
import json
import os
import shutil
import sys
import tempfile
import time
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "src"))

import instance

SHOT_DIR = Path(os.environ.get("OSHIIRE_SHOT_DIR", tempfile.gettempdir()))
SHOT_PATH = SHOT_DIR / "oshiire_quit_stopped.png"


def _pythonw():
    exe = Path(sys.executable).with_name("pythonw.exe")
    return exe if exe.exists() else None


def _build_sandbox():
    """A minimal runnable copy of the project, built ONLY from committed files
    plus a test-written .env. Never copies the real .env / manifest / data /
    staging / archive. Contents:
      - every src/*.py (the code)
      - assets/ (the bundled fonts launch_app registers)
      - the committed example configs, renamed into place:
          layout.example.json          -> layout.json
          subreddit_map.example.json   -> subreddit_map.json
          manifest.example.json        -> manifest.json
          known_series_names.example.txt -> the shortname file layout.json names
      - empty data/ and staging/ dirs (created, not copied)
      - .env with a temp ARCHIVE_DIR and NO Reddit secrets
      - a no-op .cmd so webbrowser.open launches nothing real
    """
    sandbox = Path(tempfile.mkdtemp(prefix="oshiire-quit-e2e-"))
    code = sorted((REPO / "src").glob("*.py"))
    assert code, "no src/*.py found to copy into the sandbox"
    (sandbox / "src").mkdir()
    for py in code:
        shutil.copy2(py, sandbox / "src" / py.name)
    if (REPO / "assets").is_dir():
        shutil.copytree(REPO / "assets", sandbox / "assets")

    shutil.copy2(REPO / "examples" / "layout.example.json", sandbox / "layout.json")
    shutil.copy2(REPO / "examples" / "subreddit_map.example.json", sandbox / "subreddit_map.json")
    shutil.copy2(REPO / "examples" / "manifest.example.json", sandbox / "manifest.json")
    # The shortname file name is whatever layout.json declares.
    shortname_name = json.loads(
        (sandbox / "layout.json").read_text(encoding="utf-8")
    ).get("shortname_file", "known_series_names.txt")
    shutil.copy2(REPO / "examples" / "known_series_names.example.txt", sandbox / shortname_name)

    (sandbox / "data").mkdir()
    # The sandbox has no feed link, so without this the first-run setup window
    # opens and blocks startup before the server ever records a port.
    (sandbox / "data" / "settings.json").write_text('{"setup_done": true}\n', encoding="utf-8")
    (sandbox / "staging").mkdir()
    archive = sandbox / "archive"
    archive.mkdir()
    # Minimal .env: a reachable archive folder so Home shows "connected", and no
    # secrets at all. Forward slashes so no backslash is misread as an escape.
    (sandbox / ".env").write_text(
        f"ARCHIVE_DIR={archive.as_posix()}\n", encoding="utf-8")

    noop = sandbox / "noop_browser.cmd"
    noop.write_text("@echo off\r\nexit /b 0\r\n", encoding="ascii")
    return sandbox, noop


class QuitEndToEndTest(unittest.TestCase):
    def setUp(self):
        if os.environ.get("OSHIIRE_E2E") != "1":
            self.skipTest("opt-in only: set OSHIIRE_E2E=1 to run the Quit end-to-end test")
        if sys.platform != "win32":
            self.skipTest("Windows-only: it uses pythonw.exe and a .cmd no-op browser")
        try:
            import playwright  # noqa: F401
        except ImportError:
            self.skipTest("playwright not installed (it lives in requirements-dev.txt)")
        self.pythonw = _pythonw()
        if self.pythonw is None:
            self.skipTest("pythonw.exe not found next to the interpreter")
        self.sandbox, self.noop = _build_sandbox()
        self.lock = self.sandbox / "data" / "oshiire.lock"
        self.log = self.sandbox / "data" / "oshiire.log"
        self.proc = None

    def tearDown(self):
        if self.proc is not None and self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=10)
            except Exception:
                self.proc.kill()
        shutil.rmtree(self.sandbox, ignore_errors=True)

    def _wait_for_port(self, deadline_s=90):
        deadline = time.monotonic() + deadline_s
        while time.monotonic() < deadline:
            if self.proc.poll() is not None:
                self.fail(f"app exited early (code {self.proc.returncode}); "
                          f"log:\n{self._read_log()}")
            port = instance.read_recorded_port(self.lock)
            if port is not None:
                return port
            time.sleep(0.5)
        self.fail("server never recorded a port")

    def _read_log(self):
        try:
            return self.log.read_text(encoding="utf-8", errors="replace")
        except OSError:
            return "<no log>"

    def test_quit_exits_releases_lock_and_flushes_log(self):
        import subprocess
        from playwright.sync_api import sync_playwright

        env = dict(os.environ)
        env["BROWSER"] = str(self.noop)          # no real browser opens
        env.pop("OSHIIRE_DEBUG", None)
        self.proc = subprocess.Popen(
            [str(self.pythonw), str(self.sandbox / "src" / "app.py")],
            cwd=str(self.sandbox), env=env,
        )
        port = self._wait_for_port()
        url = f"http://127.0.0.1:{port}/?__theme=light"

        with sync_playwright() as p:
            # Prefer Microsoft Edge; fall back to the bundled Chromium where Edge
            # isn't installed, so the end-to-end flow still runs. Skip only if no
            # browser can launch at all.
            try:
                browser = p.chromium.launch(channel="msedge")
            except Exception:
                try:
                    browser = p.chromium.launch()
                except Exception as exc:
                    self.skipTest(f"no Playwright browser launchable: {exc}")
            try:
                page = browser.new_page(viewport={"width": 1100, "height": 800})
                page.goto(url, wait_until="domcontentloaded")
                page.wait_for_selector("#home-page .closet", timeout=60000)
                page.wait_for_timeout(1500)
                clicked_at = time.monotonic()
                page.get_by_role("button", name="Quit Oshiire", exact=True).click()
                page.wait_for_timeout(3000)  # let the stopped page + shutdown settle
                page.screenshot(path=str(SHOT_PATH))
            finally:
                browser.close()

        # (a) the process exits on its own, within ~10s of the click.
        self.proc.wait(timeout=10)
        exit_s = time.monotonic() - clicked_at
        self.assertEqual(self.proc.returncode, 0, self._read_log())
        self.assertLess(exit_s, 10.0, f"Quit took {exit_s:.1f}s to exit")
        print(f"[quit-e2e] exit after Quit: {exit_s:.2f}s; screenshot: {SHOT_PATH}")

        # (b) the lock is released -- a fresh acquire now succeeds.
        handle = instance.acquire_single_instance(self.lock)
        self.assertIsNotNone(handle, "lock was not released after Quit")
        handle.close()

        # (c) the log is flushed and closed: readable, non-empty, and reopenable.
        self.assertTrue(self.log.exists(), "no oshiire.log was written")
        self.assertTrue(self._read_log().strip(), "oshiire.log is empty")


if __name__ == "__main__":
    unittest.main(verbosity=2)
