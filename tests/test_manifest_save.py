"""manifest.save_manifest: a temp file of its own, cleaned up on failure.

A fixed "manifest.json.tmp" let two processes saving at once rename each other's
temp file away (FileNotFoundError on the second rename). Each save now writes to
a unique temp name in the manifest's folder and renames it over the target
through fsutil.replace.

    python -m unittest tests.test_manifest_save
"""
import json
import os
import shutil
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "src"))

import fsutil
import manifest


class SaveManifestTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="manifest-save-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.path = self.tmp / "manifest.json"

    def leftovers(self):
        return sorted(p.name for p in self.tmp.iterdir() if p.name != "manifest.json")

    def test_saves_valid_json_and_leaves_no_temp_file(self):
        manifest.save_manifest({"t3_a": {"title": "日本語", "status": "pending_review"}}, self.path)
        self.assertEqual(json.loads(self.path.read_text(encoding="utf-8")),
                         {"t3_a": {"title": "日本語", "status": "pending_review"}})
        self.assertEqual(self.leftovers(), [])

    def test_each_save_uses_its_own_temp_name_in_the_same_folder(self):
        names = []
        real = fsutil.replace

        def spy(src, dst, **kw):
            names.append(Path(src))
            real(src, dst, **kw)

        with mock.patch.object(manifest.fsutil, "replace", side_effect=spy):
            manifest.save_manifest({"a": 1}, self.path)
            manifest.save_manifest({"a": 2}, self.path)
        self.assertEqual(len(names), 2)
        self.assertNotEqual(names[0].name, names[1].name)
        for tmp_path in names:
            self.assertEqual(tmp_path.parent, self.tmp)          # same folder: atomic rename
            self.assertNotEqual(tmp_path.name, "manifest.json.tmp")  # never the shared fixed name

    def test_a_failed_rename_removes_its_temp_file_and_keeps_the_old_manifest(self):
        manifest.save_manifest({"old": True}, self.path)
        with mock.patch.object(manifest.fsutil, "replace", side_effect=OSError("disk says no")):
            with self.assertRaises(OSError):
                manifest.save_manifest({"new": True}, self.path)
        self.assertEqual(self.leftovers(), [])
        self.assertEqual(json.loads(self.path.read_text(encoding="utf-8")), {"old": True})

    def test_a_failed_serialisation_removes_its_temp_file_and_keeps_the_old_manifest(self):
        manifest.save_manifest({"old": True}, self.path)
        with self.assertRaises(TypeError):
            manifest.save_manifest({"bad": object()}, self.path)
        self.assertEqual(self.leftovers(), [])
        self.assertEqual(json.loads(self.path.read_text(encoding="utf-8")), {"old": True})

    def test_the_windows_retry_in_fsutil_still_applies(self):
        """The rename goes through fsutil.replace (which retries a briefly-held file)."""
        calls = []
        real_replace = os.replace  # fsutil.os IS os: capture it before patching

        def flaky(src, dst):
            calls.append(src)
            if len(calls) < 3:
                err = PermissionError(13, "in use")
                err.winerror = 32
                raise err
            real_replace(src, dst)

        with mock.patch.object(fsutil.os, "replace", side_effect=flaky), \
                mock.patch.object(fsutil, "ATTEMPTS", 10), \
                mock.patch.object(fsutil.time, "sleep"), \
                mock.patch.object(fsutil.sys, "platform", "win32"):
            manifest.save_manifest({"ok": True}, self.path)
        self.assertEqual(len(calls), 3)
        self.assertEqual(json.loads(self.path.read_text(encoding="utf-8")), {"ok": True})
        self.assertEqual(self.leftovers(), [])

    def test_concurrent_saves_never_collide_on_the_temp_name(self):
        errors = []

        def hammer(tag):
            try:
                for i in range(25):
                    manifest.save_manifest({tag: i}, self.path)
            except Exception as exc:  # noqa: BLE001 -- the point is to see any
                errors.append(repr(exc))

        threads = [threading.Thread(target=hammer, args=(f"t{n}",)) for n in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(errors, [])
        self.assertIsInstance(json.loads(self.path.read_text(encoding="utf-8")), dict)
        self.assertEqual(self.leftovers(), [])


if __name__ == "__main__":
    unittest.main()
