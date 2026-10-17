"""Tests for datahome.py -- where the data root is.

Precedence: OSHIIRE_HOME > release marker (per-user folder for the OS) > the app
folder (a git checkout). Every OS branch is driven through data_root()'s
environ/platform/marker/home parameters, so none of them depends on the machine
running the tests.

    python -m unittest discover -s tests
"""
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "src"))

import datahome


class DataRootTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="datahome-"))
        self.marker = self.tmp / "oshiire_release.json"
        self.no_marker = self.tmp / "absent.json"
        self.home = self.tmp / "home"

    def _release(self, environ, platform):
        self.marker.write_text("{}", encoding="utf-8")
        return datahome.data_root(environ=environ, platform=platform,
                                  marker=self.marker, home=self.home)

    # -- checkout ---------------------------------------------------------------
    def test_checkout_is_the_app_folder(self):
        self.assertEqual(datahome.data_root(environ={}, platform="win32",
                                            marker=self.no_marker), datahome.APP_DIR)

    def test_this_repo_is_a_checkout(self):
        self.assertFalse(datahome.is_release())
        self.assertEqual(datahome.APP_DIR, REPO)
        self.assertEqual(datahome.RELEASE_MARKER, REPO / "oshiire_release.json")

    # -- OSHIIRE_HOME wins over everything ----------------------------------------
    def test_env_wins_over_marker_and_checkout(self):
        target = self.tmp / "custom"
        root = self._release({"OSHIIRE_HOME": str(target), "LOCALAPPDATA": str(self.tmp)}, "win32")
        self.assertEqual(root, target)

    def test_env_relative_is_made_absolute_against_cwd(self):
        prev = os.getcwd()
        try:
            os.chdir(self.tmp)
            root = datahome.data_root(environ={"OSHIIRE_HOME": "rel/data"}, marker=self.no_marker)
        finally:
            os.chdir(prev)
        self.assertTrue(root.is_absolute())
        self.assertEqual(root, Path(os.path.abspath(self.tmp / "rel" / "data")))

    def test_env_tilde_is_expanded(self):
        with mock.patch.dict(os.environ, {"USERPROFILE": str(self.home), "HOME": str(self.home)}):
            root = datahome.data_root(environ={"OSHIIRE_HOME": "~/OshiireData"},
                                      marker=self.no_marker)
        self.assertEqual(root, Path(os.path.abspath(self.home / "OshiireData")))

    def test_blank_env_is_ignored(self):
        self.assertEqual(datahome.data_root(environ={"OSHIIRE_HOME": "   "},
                                            marker=self.no_marker), datahome.APP_DIR)

    # -- release marker, per OS ---------------------------------------------------
    def test_windows_localappdata(self):
        local = self.tmp / "Local"
        self.assertEqual(self._release({"LOCALAPPDATA": str(local)}, "win32"), local / "Oshiire")

    def test_windows_without_localappdata_falls_back_to_home(self):
        self.assertEqual(self._release({}, "win32"),
                         Path(os.path.abspath(self.home / "AppData" / "Local" / "Oshiire")))

    def test_linux_xdg_data_home(self):
        xdg = self.tmp / "xdg"
        self.assertEqual(self._release({"XDG_DATA_HOME": str(xdg)}, "linux"), xdg / "oshiire")

    def test_linux_default(self):
        self.assertEqual(self._release({}, "linux"),
                         Path(os.path.abspath(self.home / ".local" / "share" / "oshiire")))

    def test_linux_relative_xdg_is_ignored(self):
        # The XDG spec: a relative XDG_DATA_HOME is invalid and must be ignored.
        self.assertEqual(self._release({"XDG_DATA_HOME": "relative/xdg"}, "linux"),
                         Path(os.path.abspath(self.home / ".local" / "share" / "oshiire")))

    def test_macos(self):
        self.assertEqual(self._release({}, "darwin"),
                         Path(os.path.abspath(self.home / "Library" / "Application Support" / "Oshiire")))

    def test_is_release_follows_the_marker(self):
        self.assertFalse(datahome.is_release(self.no_marker))
        self.marker.write_text("{}", encoding="utf-8")
        self.assertTrue(datahome.is_release(self.marker))

    def test_data_root_never_creates_anything(self):
        local = self.tmp / "Local"
        self._release({"LOCALAPPDATA": str(local)}, "win32")
        self.assertFalse(local.exists())


if __name__ == "__main__":
    unittest.main(verbosity=2)
