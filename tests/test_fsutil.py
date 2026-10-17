"""Tests for fsutil.replace: the Windows rename retry every atomic write uses.

    python -m unittest discover -s tests
"""
import ast
import os
import sys
import tempfile
import threading
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "src"))

import fsutil


def _denied(winerror):
    exc = PermissionError(13, "Access is denied")
    exc.winerror = winerror
    return exc


class FlakyReplace:
    """Fails with `error` the first `failures` calls, then succeeds."""

    def __init__(self, failures, error):
        self.failures, self.error = failures, error
        self.calls = 0

    def __call__(self, src, dst):
        self.calls += 1
        if self.calls <= self.failures:
            raise self.error
        self.done = (src, dst)


class ReplaceRetryTest(unittest.TestCase):
    def setUp(self):
        self.sleeps = []

    def _replace(self, fake, platform="win32"):
        fsutil.replace("a.tmp", "a", platform=platform, _replace=fake,
                       _sleep=self.sleeps.append)

    def test_succeeds_after_transient_failures(self):
        for winerror in (5, 32):
            with self.subTest(winerror=winerror):
                self.sleeps.clear()
                fake = FlakyReplace(3, _denied(winerror))
                self._replace(fake)
                self.assertEqual(fake.calls, 4)
                self.assertEqual(fake.done, ("a.tmp", "a"))
                self.assertEqual(len(self.sleeps), 3)

    def test_gives_up_and_reraises_the_original(self):
        original = _denied(32)
        fake = FlakyReplace(10 ** 6, original)
        with self.assertRaises(PermissionError) as caught:
            self._replace(fake)
        self.assertIs(caught.exception, original)
        self.assertEqual(fake.calls, fsutil.ATTEMPTS)
        self.assertEqual(len(self.sleeps), fsutil.ATTEMPTS - 1)
        self.assertAlmostEqual(sum(self.sleeps), 1.0, delta=0.05)

    def test_other_errors_are_not_retried(self):
        for error in (_denied(None), _denied(1224), FileNotFoundError(2, "gone"),
                      OSError(28, "No space left")):
            with self.subTest(error=error):
                self.sleeps.clear()
                fake = FlakyReplace(1, error)
                with self.assertRaises(type(error)):
                    self._replace(fake)
                self.assertEqual(fake.calls, 1)
                self.assertEqual(self.sleeps, [])

    def test_no_retry_off_windows(self):
        fake = FlakyReplace(1, _denied(32))
        with self.assertRaises(PermissionError):
            self._replace(fake, platform="linux")
        self.assertEqual(fake.calls, 1)
        self.assertEqual(self.sleeps, [])

    def test_real_replace(self):
        with tempfile.TemporaryDirectory() as tmp:
            src, dst = Path(tmp) / "x.tmp", Path(tmp) / "x"
            dst.write_text("old", encoding="utf-8")
            src.write_text("new", encoding="utf-8")
            fsutil.replace(src, dst)
            self.assertEqual(dst.read_text(encoding="utf-8"), "new")
            self.assertFalse(src.exists())


@unittest.skipUnless(sys.platform == "win32", "Windows file-sharing semantics")
class HeldOpenOnWindowsTest(unittest.TestCase):
    """The real failure: a reader holds the target open (Python's open() does
    not share delete), so a plain os.replace onto it is refused; the retry
    gets through once the reader lets go."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="fsutil-held-"))
        self.src, self.dst = self.tmp / "x.tmp", self.tmp / "x"
        self.dst.write_text("old", encoding="utf-8")
        self.src.write_text("new", encoding="utf-8")

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_plain_replace_is_refused_while_held(self):
        with self.dst.open("rb"):
            with self.assertRaises(PermissionError) as caught:
                os.replace(self.src, self.dst)
        self.assertIn(caught.exception.winerror, fsutil.RETRY_WINERRORS)

    def test_retry_succeeds_once_the_reader_closes(self):
        reader = self.dst.open("rb")
        closer = threading.Timer(0.15, reader.close)
        closer.start()
        try:
            fsutil.replace(self.src, self.dst)
        finally:
            closer.join()
            reader.close()
        self.assertEqual(self.dst.read_text(encoding="utf-8"), "new")


class NoBareReplaceInAppCodeTest(unittest.TestCase):
    """Every atomic write goes through fsutil.replace: no src/ app module
    calls os.replace itself (fsutil is the one place it is allowed)."""

    def test_no_os_replace_outside_fsutil(self):
        offenders = []
        modules = sorted((REPO / "src").glob("*.py"))
        self.assertTrue(modules, "found no src/*.py -- the guard would check nothing")
        for path in modules:
            if path.name == "fsutil.py":
                continue
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            for node in ast.walk(tree):
                if (isinstance(node, ast.Attribute) and node.attr == "replace"
                        and isinstance(node.value, ast.Name) and node.value.id == "os"):
                    offenders.append(f"{path.name}:{node.lineno}")
        self.assertEqual(offenders, [])


if __name__ == "__main__":
    unittest.main(verbosity=2)
