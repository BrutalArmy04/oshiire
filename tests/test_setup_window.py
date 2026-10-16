"""Tests for setup_window.py -- the first-run / "Change setup" window.

The logic (validators, needs_setup, the feed test, the shortcut builders,
Save/Skip/Cancel) is tested without a display. The window itself is driven
programmatically and skipped cleanly when Tk can't open one. Nothing here
creates a real Start-menu entry, .desktop file or shortcut, and no test reads
the real .env (config_sandbox).

    python -m unittest discover -s tests
"""
import base64
import contextlib
import io
import json
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(Path(__file__).resolve().parent))  # config_sandbox

import config
import datahome
import settings
import setup_window as sw
from config_sandbox import isolated_config

TOKEN = "a1b2c3d4e5f6SECRETTOKEN"
GOOD = f"https://old.reddit.com/saved.rss?feed={TOKEN}&user=some_user"


class FeedValidatorTest(unittest.TestCase):
    def _check(self, text, ok, message=None):
        result = sw.validate_feed_url(text)
        self.assertEqual(result[0], ok, (text, result[2]))
        if message is not None:
            self.assertEqual(result[2], message)
        # Never echo the link or its token.
        self.assertNotIn(TOKEN, result[2])
        self.assertNotIn("saved.rss?", result[2])
        return result

    def test_host_rewrite_for_every_reddit_host(self):
        for host in ("old.reddit.com", "www.reddit.com", "reddit.com", "OLD.Reddit.com"):
            ok, normalized, _ = self._check(
                f"https://{host}/saved.rss?feed={TOKEN}&user=some_user", True, sw.MSG_FEED_OK)
            self.assertEqual(normalized,
                             f"https://www.reddit.com/saved.rss?feed={TOKEN}&user=some_user")

    def test_param_order_and_extras_kept_as_pasted(self):
        ok, normalized, _ = self._check(
            f"  https://www.reddit.com/saved.rss/?user=me_too&feed={TOKEN}&limit=100#top  ", True)
        self.assertEqual(normalized,
                         f"https://www.reddit.com/saved.rss?user=me_too&feed={TOKEN}&limit=100")

    def test_surrounding_quotes_are_ignored(self):
        self._check(f'"{GOOD}"', True)

    def test_each_hint(self):
        cases = [
            ("", sw.HINT_EMPTY),
            ("   ", sw.HINT_EMPTY),
            ("some_user", sw.HINT_USERNAME),
            ("u/some_user", sw.HINT_USERNAME),
            ("/u/some_user/", sw.HINT_USERNAME),
            (GOOD.replace("https://", "http://"), sw.HINT_SCHEME),
            (GOOD.replace("https://", ""), sw.HINT_SCHEME),
            (GOOD.replace("old.reddit.com", "reddit.example.com"), sw.HINT_HOST),
            ("https://old.reddit.com/prefs/feeds/", sw.HINT_FEEDS_PAGE),
            ("https://www.reddit.com/prefs/feeds", sw.HINT_FEEDS_PAGE),
            ("https://www.reddit.com/user/some_user/saved/", sw.HINT_USER_PAGE),
            ("https://old.reddit.com/u/some_user", sw.HINT_USER_PAGE),
            (f"https://www.reddit.com/.rss?feed={TOKEN}&user=x", sw.HINT_OTHER_FEED),
            (f"https://www.reddit.com/saved.json?feed={TOKEN}&user=x", sw.HINT_OTHER_FEED),
            (f"https://www.reddit.com/saved.rss?feed={TOKEN}", sw.HINT_PARAMS),
            ("https://www.reddit.com/saved.rss?user=x", sw.HINT_PARAMS),
            ("https://www.reddit.com/saved.rss?feed=&user=x", sw.HINT_PARAMS),
        ]
        for text, hint in cases:
            with self.subTest(text=text):
                ok, normalized, message = self._check(text, False, hint)
                self.assertEqual(normalized, "")

    def test_user_param_prefills_the_username(self):
        self.assertEqual(sw.feed_user(GOOD), "some_user")
        self.assertEqual(sw.feed_user(f"https://www.reddit.com/saved.rss?feed={TOKEN}"), "")


class ArchiveValidatorTest(unittest.TestCase):
    def setUp(self):
        self.base = Path(tempfile.mkdtemp(prefix="sw-arch-")).resolve()
        self.app = self.base / "Apps" / "Oshiire"
        self.data = self.base / "Data" / "Oshiire"
        self.app.mkdir(parents=True)
        self.data.mkdir(parents=True)

    def tearDown(self):
        shutil.rmtree(self.base, ignore_errors=True)

    def _check(self, path):
        return sw.validate_archive_dir(path, self.app, self.data)

    def test_empty_and_relative(self):
        self.assertEqual(self._check("  ").message, sw.MSG_ARCHIVE_EMPTY)
        self.assertEqual(self._check("Pictures/Art").message, sw.MSG_ARCHIVE_RELATIVE)

    def test_equal_to_or_containing_the_app_folder_is_refused(self):
        for path in (self.app, self.app.parent, self.base):
            with self.subTest(path=path):
                check = self._check(str(path))
                self.assertFalse(check.ok)
                self.assertEqual(check.message, sw.MSG_ARCHIVE_INSIDE_APP)

    def test_equal_to_or_containing_the_data_root_is_refused(self):
        for path in (self.data, self.data.parent):
            with self.subTest(path=path):
                check = self._check(str(path))
                self.assertFalse(check.ok)
                self.assertEqual(check.message, sw.MSG_ARCHIVE_INSIDE_DATA)

    def test_a_folder_inside_the_app_or_data_folder_is_allowed(self):
        # Only the archive itself is served; a child folder can't reach .env.
        for path in (self.app / "archive", self.data / "pictures"):
            path.mkdir()
            self.assertTrue(self._check(str(path)).ok, path)

    def test_trailing_slash_variants(self):
        for text in (str(self.app) + os.sep, str(self.app) + "/", str(self.data) + os.sep + os.sep):
            with self.subTest(text=text):
                self.assertFalse(self._check(text).ok)

    @unittest.skipUnless(sys.platform == "win32", "case-insensitive paths are a Windows thing")
    def test_case_variants_on_windows(self):
        for text in (str(self.app).upper(), str(self.app.parent).lower(), str(self.data).swapcase()):
            with self.subTest(text=text):
                self.assertFalse(self._check(text).ok)

    def test_existing_writable_folder_is_ok(self):
        target = self.base / "My Archive"
        target.mkdir()
        check = self._check(f'"{target}"')  # Explorer's "Copy as path" quotes
        self.assertEqual((check.ok, check.will_create), (True, False))
        self.assertEqual(list(target.iterdir()), [])  # the write probe cleaned up

    def test_missing_folder_will_be_created_after_confirm(self):
        check = self._check(str(self.base / "New" / "Archive"))
        self.assertEqual((check.ok, check.will_create, check.message),
                         (True, True, sw.MSG_ARCHIVE_CREATE))
        self.assertFalse((self.base / "New").exists())  # the validator creates nothing

    def test_a_file_is_refused(self):
        f = self.base / "notes.txt"
        f.write_text("x", encoding="utf-8")
        self.assertEqual(self._check(str(f)).message, sw.MSG_ARCHIVE_FILE)

    def test_unwritable_folder_is_refused(self):
        target = self.base / "ReadOnly"
        target.mkdir()
        with mock.patch.object(sw.tempfile, "mkstemp", side_effect=PermissionError):
            self.assertEqual(self._check(str(target)).message, sw.MSG_ARCHIVE_READONLY)


class _Sandbox(unittest.TestCase):
    """A temp data root (cwd) with no real .env anywhere in reach."""

    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix="sw-root-")).resolve()
        self._ctx = isolated_config(self.root)
        self.app_dir = self._ctx.__enter__()

    def tearDown(self):
        self._ctx.__exit__(None, None, None)
        shutil.rmtree(self.root, ignore_errors=True)

    def settings_json(self):
        path = self.root / "data" / "settings.json"
        return json.loads(path.read_text(encoding="utf-8")) if path.exists() else None

    def files(self):
        return sorted(str(p.relative_to(self.root)) for p in self.root.rglob("*") if p.is_file())


class NeedsSetupTest(_Sandbox):
    def test_truth_table(self):
        for feed in (False, True):
            for archive in (False, True):
                for done in (False, True):
                    with self.subTest(feed=feed, archive=archive, done=done):
                        for key in sw.REQUIRED_KEYS:
                            os.environ.pop(key, None)
                        if feed:
                            os.environ[sw.FEED_KEY] = GOOD
                        if archive:
                            os.environ[sw.ARCHIVE_KEY] = str(self.root)
                        settings.save_setup_done(done)
                        expected = (not (feed and archive)) and not done
                        self.assertEqual(sw.needs_setup(), expected)

    def test_setup_done_must_be_a_real_true(self):
        (self.root / "data").mkdir(exist_ok=True)
        (self.root / "data" / "settings.json").write_text('{"setup_done": "yes"}', encoding="utf-8")
        self.assertTrue(sw.needs_setup())

    def test_values_from_config_env_count(self):
        config.save_setup({sw.FEED_KEY: GOOD, sw.ARCHIVE_KEY: str(self.root)})
        self.assertFalse(sw.needs_setup())

    def test_setup_done_setter_keeps_other_settings(self):
        settings.save_theme("dark")
        settings.save_setup_done(True)
        self.assertEqual(self.settings_json(), {"theme": "dark", "setup_done": True})
        self.assertTrue(settings.setup_done())
        settings.save_setup_done(False)
        self.assertFalse(settings.setup_done())


class SaveSkipLogicTest(_Sandbox):
    def test_apply_save_writes_only_editable_keys(self):
        (self.root / ".env").write_text(f"{sw.ARCHIVE_KEY}=D:/from-dotenv\n", encoding="utf-8")
        written = sw.apply_save({sw.FEED_KEY: GOOD, sw.ARCHIVE_KEY: "E:/typed", sw.USER_KEY: "me"})
        self.assertEqual(set(written), {sw.FEED_KEY, sw.USER_KEY})
        raw = (self.root / "data" / "config.env").read_text(encoding="utf-8")
        self.assertNotIn(sw.ARCHIVE_KEY, raw)
        self.assertEqual(config.get(sw.ARCHIVE_KEY), "D:/from-dotenv")
        self.assertTrue(settings.setup_done())
        self.assertFalse((self.root / ".env").read_text(encoding="utf-8").count("\n") > 1)

    def test_apply_skip_writes_only_setup_done(self):
        sw.apply_skip()
        self.assertEqual(self.files(), [os.path.join("data", "settings.json")])
        self.assertEqual(self.settings_json(), {"setup_done": True})


class FeedTestTest(unittest.TestCase):
    """test_feed goes through ingest.fetch_feed and never shows the link."""

    def _run(self, effect=None, result=None):
        import ingest
        with mock.patch.object(ingest, "fetch_feed", side_effect=effect,
                               return_value=result) as fetch:
            ok, message = sw.test_feed(GOOD)
        self.assertNotIn(TOKEN, message)
        self.assertNotIn("reddit.com", message)
        return ok, message, fetch

    def test_found_posts_and_uses_the_normalized_link(self):
        feed = mock.Mock(entries=[1, 2, 3], bozo=False)
        ok, message, fetch = self._run(result=feed)
        self.assertEqual((ok, message), (True, "Found 3 saved posts. The link works."))
        fetch.assert_called_once_with(GOOD.replace("old.reddit.com", "www.reddit.com"))

    def test_singular(self):
        ok, message, _ = self._run(result=mock.Mock(entries=[1], bozo=False))
        self.assertEqual(message, "Found 1 saved post. The link works.")

    def test_reasons(self):
        import ingest
        import requests
        refused = requests.HTTPError(f"403 for url {GOOD}")
        refused.response = mock.Mock(status_code=403)
        busy = requests.HTTPError(f"503 for url {GOOD}")
        busy.response = mock.Mock(status_code=503)
        cases = [
            (ingest.FeedUnavailable("wall"), sw.MSG_TEST_WALL),
            (refused, sw.MSG_TEST_REFUSED),
            (requests.Timeout(f"timed out {GOOD}"), sw.MSG_TEST_TIMEOUT),
            (requests.ConnectionError(f"no route {GOOD}"), sw.MSG_TEST_OFFLINE),
            (RuntimeError(f"weird {GOOD}"), "The test failed (RuntimeError)."),
        ]
        for exc, expected in cases:
            with self.subTest(exc=type(exc).__name__):
                ok, message, _ = self._run(effect=exc)
                self.assertFalse(ok)
                self.assertEqual(message, expected)
        ok, message, _ = self._run(effect=busy)
        self.assertIn("error 503", message)

    def test_not_a_feed(self):
        ok, message, _ = self._run(result=mock.Mock(entries=[], bozo=True))
        self.assertEqual((ok, message), (False, sw.MSG_TEST_NOT_FEED))

    def test_invalid_link_is_never_fetched(self):
        import ingest
        with mock.patch.object(ingest, "fetch_feed") as fetch:
            ok, message = sw.test_feed("https://old.reddit.com/prefs/feeds/")
        fetch.assert_not_called()
        self.assertEqual((ok, message), (False, sw.HINT_FEEDS_PAGE))


class ShortcutBuilderTest(unittest.TestCase):
    """Builders only: they return what would be done; nothing is created."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="sw-sc-")).resolve()
        self.app = self.tmp / "Oshi ire's $app"
        (self.app / "assets").mkdir(parents=True)
        self.exe_dir = self.tmp / "runtime"
        self.exe_dir.mkdir()
        (self.exe_dir / "python.exe").write_text("", encoding="utf-8")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_windows_shortcut(self):
        (self.exe_dir / "pythonw.exe").write_text("", encoding="utf-8")
        spec = sw.build_windows_shortcut(app_dir=self.app, executable=self.exe_dir / "python.exe",
                                         environ={"APPDATA": str(self.tmp / "Roaming")})
        self.assertEqual(spec["path"], self.tmp / "Roaming" / "Microsoft" / "Windows" /
                         "Start Menu" / "Programs" / "Oshiire.lnk")
        self.assertEqual(spec["target"], self.exe_dir / "pythonw.exe")
        self.assertEqual(spec["arguments"], f'"{self.app / "app.py"}"')
        self.assertEqual(spec["workdir"], self.app)
        self.assertIsNone(spec["icon"])
        self.assertNotIn("IconLocation", spec["script"])
        self.assertEqual(spec["argv"][0], "powershell.exe")
        decoded = base64.b64decode(spec["argv"][-1]).decode("utf-16-le")
        self.assertEqual(decoded, spec["script"])
        self.assertIn("WScript.Shell", decoded)
        self.assertIn("$s.Save()", decoded)
        self.assertIn("Oshi ire''s $app", decoded)  # single quote doubled in PS literal
        self.assertFalse(spec["path"].exists())

    def test_windows_icon_only_when_one_exists(self):
        (self.app / "assets" / "oshiire.ico").write_bytes(b"\0")
        spec = sw.build_windows_shortcut(app_dir=self.app, executable=self.exe_dir / "python.exe",
                                         environ={"APPDATA": str(self.tmp)})
        self.assertEqual(spec["icon"], self.app / "assets" / "oshiire.ico")
        self.assertIn("IconLocation", spec["script"])
        # No pythonw sibling -> the interpreter itself.
        self.assertEqual(spec["target"], self.exe_dir / "python.exe")

    def test_linux_desktop_file(self):
        spec = sw.build_linux_desktop(app_dir=self.app, environ={}, home=self.tmp / "home")
        self.assertEqual(spec["path"], self.tmp / "home" / ".local" / "share" / "applications" /
                         "oshiire.desktop")
        lines = spec["content"].splitlines()
        self.assertEqual(lines[0], "[Desktop Entry]")
        exec_line = [l for l in lines if l.startswith("Exec=")][0]
        self.assertTrue(exec_line.startswith('Exec="') and exec_line.endswith('oshiire.sh"'))
        self.assertIn(r"\$app", exec_line)  # $ escaped per the Desktop Entry spec
        self.assertIn("Terminal=false", lines)
        self.assertFalse(any(l.startswith("Icon=") for l in lines))
        self.assertFalse(spec["path"].exists())

    def test_linux_xdg_data_home(self):
        spec = sw.build_linux_desktop(app_dir=self.app, environ={"XDG_DATA_HOME": str(self.tmp / "x")})
        self.assertEqual(spec["path"], self.tmp / "x" / "applications" / "oshiire.desktop")

    def test_create_on_windows_runs_powershell_via_the_runner(self):
        runner = mock.Mock(return_value=mock.Mock(returncode=0))
        with mock.patch.dict(os.environ, {"APPDATA": str(self.tmp / "Roaming")}):
            ok, message = sw.create_shortcut(platform="win32", runner=runner)
        self.assertTrue(ok)
        self.assertEqual(runner.call_args.args[0][0], "powershell.exe")
        self.assertFalse((self.tmp / "Roaming" / "Microsoft" / "Windows" / "Start Menu" /
                          "Programs" / "Oshiire.lnk").exists())  # the mock created nothing

    def test_create_on_linux_writes_into_the_sandboxed_data_home(self):
        with mock.patch.dict(os.environ, {"XDG_DATA_HOME": str(self.tmp / "share")}):
            ok, _ = sw.create_shortcut(platform="linux")
        self.assertTrue(ok)
        self.assertTrue((self.tmp / "share" / "applications" / "oshiire.desktop").is_file())

    def test_labels(self):
        self.assertEqual(sw.shortcut_label("win32"), "Add Oshiire to the Start menu")
        self.assertEqual(sw.shortcut_label("linux"), "Add Oshiire to the app menu")
        self.assertFalse(sw.shortcut_supported("darwin"))


class ModelFilesTest(unittest.TestCase):
    def test_model_files_come_from_imgutils_constants(self):
        import inspect
        import ai_tagger
        from imgutils.tagging import wd14
        self.assertEqual(ai_tagger.model_files(), [
            ("deepghs/wd14_tagger_with_embeddings", "SmilingWolf/wd-swinv2-tagger-v3/model.onnx"),
            ("SmilingWolf/wd-swinv2-tagger-v3", "selected_tags.csv"),
        ])
        # The one repo id imgutils has no constant for must still be the one its
        # loader uses -- an imgutils upgrade that changes it fails here.
        self.assertIn(repr(ai_tagger.WD14_EMBEDDINGS_REPO),
                      inspect.getsource(wd14._get_wd14_model))

    def test_download_runs_in_a_child_process(self):
        runner = mock.Mock(return_value=mock.Mock(returncode=0))
        ok, message = sw.download_model_in_child(executable="py.exe", runner=runner)
        self.assertTrue(ok)
        argv = runner.call_args.args[0]
        self.assertEqual(argv, ["py.exe", str(datahome.APP_DIR / "setup_window.py"),
                                "--download-model"])
        runner.return_value = mock.Mock(returncode=1)
        self.assertFalse(sw.download_model_in_child(executable="py.exe", runner=runner)[0])

    def test_ai_tag_announces_the_download_once(self):
        import ai_tag
        manifest = {}
        tmp = Path(tempfile.mkdtemp(prefix="sw-ai-"))
        for i in range(2):
            img = tmp / f"{i}.png"
            img.write_bytes(b"x")
            manifest[f"t3_{i}"] = {"status": "pending_review", "local_path": str(img),
                                   "guess_confidence": "low", "franchise": []}
        from types import SimpleNamespace
        guess = SimpleNamespace(name=[], confidence="low")
        for cached, expected in ((False, 1), (True, 0)):
            with self.subTest(cached=cached), \
                 mock.patch.object(ai_tag, "model_is_cached", return_value=cached), \
                 mock.patch.object(ai_tag, "guess_franchise_and_character_ai",
                                   return_value=([], guess)), \
                 contextlib.redirect_stdout(io.StringIO()) as out:
                for e in manifest.values():
                    e["guess_source"] = None
                ai_tag.run_ai_tagging(manifest)
            self.assertEqual(out.getvalue().count(ai_tag.DOWNLOAD_NOTICE), expected)
        shutil.rmtree(tmp, ignore_errors=True)


class ChangeModeEntryTest(unittest.TestCase):
    def test_change_loads_config_then_opens_the_change_window(self):
        calls = []
        with mock.patch.object(sw.config, "load", side_effect=lambda: calls.append("load")), \
             mock.patch.object(sw, "run_setup_window",
                               side_effect=lambda mode: calls.append(mode) or "saved"), \
             contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(sw.main(["--change"]), 0)
        self.assertEqual(calls, ["load", "change"])


def _tk_root():
    try:
        import tkinter
        root = tkinter.Tk()
        root.withdraw()
        return root
    except Exception as exc:  # no tkinter, or no display
        raise unittest.SkipTest(f"no Tk display ({type(exc).__name__})")


class WindowTest(_Sandbox):
    """The real window, driven programmatically (never shown or mainlooped)."""

    def setUp(self):
        super().setUp()
        self.tk_root = _tk_root()

    def tearDown(self):
        try:
            self.tk_root.destroy()
        except Exception:
            pass
        super().tearDown()

    def _window(self, mode="first", confirm=None):
        return sw.SetupWindow(mode=mode, root=self.tk_root, confirm=confirm or (lambda *a: True))

    def test_save_writes_config_and_setup_done(self):
        archive = self.root.parent / f"{self.root.name}-archive"
        archive.mkdir()
        self.addCleanup(shutil.rmtree, archive, True)
        win = self._window()
        win.shortcut_var.set(False)
        win.feed_var.set(GOOD)
        win.archive_var.set(str(archive))
        self.assertEqual(win.user_var.get(), "some_user")  # pre-filled from user=
        with mock.patch.object(sw, "create_shortcut") as shortcut:
            self.assertTrue(win.save())
        shortcut.assert_not_called()
        self.assertEqual(win.result, sw.RESULT_SAVED)
        self.assertEqual(config.get(sw.FEED_KEY), GOOD.replace("old.reddit.com", "www.reddit.com"))
        self.assertEqual(config.get(sw.ARCHIVE_KEY), str(archive))
        self.assertEqual(config.get(sw.USER_KEY), "some_user")
        self.assertTrue(settings.setup_done())

    def test_save_with_shortcut_checked_creates_it(self):
        win = self._window()
        win.shortcut_var.set(True)
        with mock.patch.object(sw, "create_shortcut", return_value=(True, "ok")) as shortcut, \
             contextlib.redirect_stdout(io.StringIO()):
            win.save()
        self.assertEqual(shortcut.call_count, 1 if sw.shortcut_supported() else 0)

    def test_save_refuses_a_bad_link_and_writes_nothing(self):
        win = self._window()
        win.feed_var.set("https://old.reddit.com/prefs/feeds/")
        self.assertFalse(win.save())
        self.assertEqual(win.feed_msg.cget("text"), sw.HINT_FEEDS_PAGE)
        self.assertEqual(self.files(), [])
        self.assertNotEqual(win.result, sw.RESULT_SAVED)

    def test_missing_archive_is_created_only_after_confirm(self):
        target = self.root.parent / f"{self.root.name}-new" / "Archive"
        self.addCleanup(shutil.rmtree, target.parent, True)
        win = self._window(confirm=lambda *a: False)
        win.archive_var.set(str(target))
        self.assertFalse(win.save())
        self.assertFalse(target.exists())
        self.assertEqual(self.files(), [])
        win = self._window(confirm=lambda *a: True)
        win.archive_var.set(str(target))
        self.assertTrue(win.save())
        self.assertTrue(target.is_dir())

    def test_skip_writes_only_setup_done(self):
        win = self._window()
        win.feed_var.set(GOOD)  # typed but skipped -> not saved
        win.skip()
        self.assertEqual(win.result, sw.RESULT_SKIPPED)
        self.assertEqual(self.files(), [os.path.join("data", "settings.json")])
        self.assertEqual(self.settings_json(), {"setup_done": True})

    def test_cancel_writes_nothing(self):
        win = self._window()
        win.feed_var.set(GOOD)
        win.cancel()
        self.assertEqual(win.result, sw.RESULT_CANCELLED)
        self.assertEqual(self.files(), [])

    def test_closing_the_window_is_cancel(self):
        win = self._window()
        self.tk_root.protocol("WM_DELETE_WINDOW")  # the registered handler exists
        win.cancel()
        self.assertEqual(win.result, sw.RESULT_CANCELLED)
        self.assertEqual(self.files(), [])

    def test_dotenv_keys_are_read_only_and_never_shown(self):
        secret_archive = "Z:/secret/place"
        (self.root / ".env").write_text(f"{sw.ARCHIVE_KEY}={secret_archive}\n", encoding="utf-8")
        win = self._window()
        texts = []

        def walk(widget):
            for child in widget.winfo_children():
                try:
                    texts.append(str(child.cget("text")))
                except Exception:
                    pass
                walk(child)
        walk(self.tk_root)
        self.assertTrue(any(t.startswith("Set in .env") for t in texts))
        self.assertFalse(any(secret_archive in t for t in texts))
        self.assertEqual(win.archive_var.get(), "")
        win.feed_var.set(GOOD)
        win.save()
        self.assertEqual(config.get(sw.ARCHIVE_KEY), secret_archive)  # untouched
        self.assertNotIn(sw.ARCHIVE_KEY,
                         (self.root / "data" / "config.env").read_text(encoding="utf-8"))

    def test_change_mode_has_no_skip_and_no_extras(self):
        win = self._window(mode="change")
        labels = []

        def walk(widget):
            for child in widget.winfo_children():
                try:
                    labels.append(str(child.cget("text")))
                except Exception:
                    pass
                walk(child)
        walk(self.tk_root)
        self.assertNotIn("Skip for now", labels)
        self.assertFalse(any("AI model" in t for t in labels))
        self.assertFalse(any("Start menu" in t or "app menu" in t for t in labels))
        self.assertIn("Save", labels)
        self.assertIn("Cancel", labels)

    def test_feed_is_masked_until_show(self):
        win = self._window()
        self.assertEqual(win.feed_entry.cget("show"), "•")
        win.show_var.set(True)
        win._toggle_show()
        self.assertEqual(win.feed_entry.cget("show"), "")


if __name__ == "__main__":
    unittest.main(verbosity=2)
