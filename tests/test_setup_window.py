"""Tests for setup_window.py -- the first-run / "Change setup" window.

The logic (validators, needs_setup, the login test, the shortcut builders,
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
sys.path.insert(0, str(REPO / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))  # config_sandbox

import config
import datahome
import settings
import setup_window as sw
from config_sandbox import isolated_config
from listing_fixture import FakeResponse, forbidden, page, thing

SESSION = "SECRETSESSIONVALUE123"
COOKIE = f"reddit_session={SESSION}; over18=1"
USER = "some_user"


class UsernameValidatorTest(unittest.TestCase):
    def test_accepted_spellings_give_the_bare_name(self):
        for text in ("some_user", "  some_user ", "u/some_user", "/user/some_user/", "/u/some_user"):
            with self.subTest(text=text):
                self.assertEqual(sw.clean_username(text), (True, "some_user", ""))

    def test_empty_and_bad_names(self):
        self.assertEqual(sw.clean_username("")[::2], (False, sw.HINT_USER_EMPTY))
        self.assertEqual(sw.clean_username("   ")[::2], (False, sw.HINT_USER_EMPTY))
        for text in ("ab", "x" * 21, "has space", "https://www.reddit.com/user/me/", "a@b.c"):
            with self.subTest(text=text):
                self.assertEqual(sw.clean_username(text)[::2], (False, sw.HINT_USER_BAD))


class CookieValidatorTest(unittest.TestCase):
    def test_plain_value_is_kept_as_pasted(self):
        self.assertEqual(sw.clean_cookie(COOKIE), (True, COOKIE, ""))

    def test_cookie_prefix_and_quotes_are_stripped(self):
        for text in (f"Cookie: {COOKIE}", f"cookie:{COOKIE}", f'"{COOKIE}"', f"'{COOKIE}'",
                     f'  "Cookie: {COOKIE}"  ', f"COOKIE :   {COOKIE}\n"):
            with self.subTest(text=text):
                self.assertEqual(sw.clean_cookie(text), (True, COOKIE, ""))

    def test_session_cookie_may_come_anywhere_in_the_string(self):
        value = f"loid=abc; reddit_session={SESSION}; token_v2=zzz"
        self.assertEqual(sw.clean_cookie(value), (True, value, ""))

    def test_rejections_never_echo_the_value(self):
        cases = [
            ("", sw.HINT_COOKIE_EMPTY),
            ("   ", sw.HINT_COOKIE_EMPTY),
            ("Cookie:", sw.HINT_COOKIE_EMPTY),
            ("over18=1; loid=SECRETOTHERVALUE", sw.HINT_COOKIE_BAD),
            ("reddit_session=", sw.HINT_COOKIE_BAD),
            ("xreddit_session=SECRETOTHERVALUE", sw.HINT_COOKIE_BAD),
            (f"{COOKIE}\nsecond=line", sw.HINT_COOKIE_LINES),
        ]
        for text, hint in cases:
            with self.subTest(text=text[:20]):
                ok, cookie, message = sw.clean_cookie(text)
                self.assertEqual((ok, cookie, message), (False, "", hint))
                self.assertNotIn("SECRET", message)


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
    def test_required_keys(self):
        self.assertEqual(set(sw.REQUIRED_KEYS),
                         {"ARCHIVE_DIR", "REDDIT_USERNAME", "REDDIT_SESSION_COOKIE"})

    def test_truth_table(self):
        import itertools
        for user, cookie, archive, done in itertools.product((False, True), repeat=4):
            with self.subTest(user=user, cookie=cookie, archive=archive, done=done):
                for key in sw.REQUIRED_KEYS:
                    os.environ.pop(key, None)
                if user:
                    os.environ[sw.USER_KEY] = USER
                if cookie:
                    os.environ[sw.COOKIE_KEY] = COOKIE
                if archive:
                    os.environ[sw.ARCHIVE_KEY] = str(self.root)
                settings.save_setup_done(done)
                expected = (not (user and cookie and archive)) and not done
                self.assertEqual(sw.needs_setup(), expected)

    def test_the_old_feed_url_no_longer_satisfies_setup(self):
        os.environ["REDDIT_SAVED_FEED_URL"] = "https://www.reddit.com/saved.rss?feed=x&user=y"
        os.environ[sw.ARCHIVE_KEY] = str(self.root)
        self.assertTrue(sw.needs_setup())

    FEED = "https://www.reddit.com/saved.rss?feed=x&user=y"

    def _upgrader(self, user=False, cookie=False):
        """A 1.0.x user: feed link and archive folder set, setup already answered."""
        os.environ[sw.FEED_KEY] = self.FEED
        os.environ[sw.ARCHIVE_KEY] = str(self.root)
        if user:
            os.environ[sw.USER_KEY] = USER
        if cookie:
            os.environ[sw.COOKIE_KEY] = COOKIE
        settings.save_setup_done(True)

    def test_upgrader_from_the_feed_is_prompted_despite_setup_done(self):
        for user, cookie in ((False, False), (True, False), (False, True)):
            with self.subTest(user=user, cookie=cookie):
                for key in (sw.USER_KEY, sw.COOKIE_KEY):
                    os.environ.pop(key, None)
                self._upgrader(user, cookie)
                self.assertTrue(sw.coming_from_feed())
                self.assertTrue(sw.needs_setup())

    def test_upgrader_with_username_and_cookie_is_not_prompted(self):
        self._upgrader(user=True, cookie=True)
        self.assertFalse(sw.coming_from_feed())
        self.assertFalse(sw.needs_setup())

    def test_upgrader_is_prompted_once_skip_or_save(self):
        for answer in ("skip", "save"):
            with self.subTest(answer=answer):
                for key in (sw.USER_KEY, sw.COOKIE_KEY):
                    os.environ.pop(key, None)
                self._upgrader()
                settings.save_feed_migration_done(False)
                self.assertTrue(sw.needs_setup())
                if answer == "skip":
                    sw.apply_skip()
                else:
                    sw.apply_save({sw.USER_KEY: USER})  # cookie still missing
                self.assertTrue(settings.feed_migration_done())
                self.assertFalse(sw.needs_setup())

    def test_a_cancel_does_not_end_the_prompt(self):
        self._upgrader()
        self.assertFalse(settings.feed_migration_done())
        self.assertTrue(sw.needs_setup())

    def test_fresh_install_skip_is_never_reopened(self):
        os.environ[sw.ARCHIVE_KEY] = str(self.root)
        self.assertTrue(sw.needs_setup())
        sw.apply_skip()
        self.assertFalse(sw.coming_from_feed())
        self.assertFalse(sw.needs_setup())
        self.assertEqual(self.settings_json(), {"setup_done": True})

    def test_an_empty_feed_key_is_not_an_upgrader(self):
        os.environ[sw.FEED_KEY] = "  "
        os.environ[sw.ARCHIVE_KEY] = str(self.root)
        settings.save_setup_done(True)
        self.assertFalse(sw.needs_setup())

    def test_setup_done_must_be_a_real_true(self):
        (self.root / "data").mkdir(exist_ok=True)
        (self.root / "data" / "settings.json").write_text('{"setup_done": "yes"}', encoding="utf-8")
        self.assertTrue(sw.needs_setup())

    def test_values_from_config_env_count(self):
        config.save_setup({sw.USER_KEY: USER, sw.COOKIE_KEY: COOKIE,
                           sw.ARCHIVE_KEY: str(self.root)})
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
        written = sw.apply_save({sw.COOKIE_KEY: COOKIE, sw.ARCHIVE_KEY: "E:/typed", sw.USER_KEY: "me"})
        self.assertEqual(set(written), {sw.COOKIE_KEY, sw.USER_KEY})
        raw = (self.root / "data" / "config.env").read_text(encoding="utf-8")
        self.assertNotIn(sw.ARCHIVE_KEY, raw)
        self.assertEqual(config.get(sw.ARCHIVE_KEY), "D:/from-dotenv")
        self.assertTrue(settings.setup_done())
        self.assertFalse((self.root / ".env").read_text(encoding="utf-8").count("\n") > 1)

    def test_apply_skip_writes_only_setup_done(self):
        sw.apply_skip()
        self.assertEqual(self.files(), [os.path.join("data", "settings.json")])
        self.assertEqual(self.settings_json(), {"setup_done": True})


class LoginTestTest(unittest.TestCase):
    """test_login makes ONE saved-listing fetch with the typed cookie and never
    shows the cookie."""

    def _run(self, effect=None, result=None, username=USER, cookie=COOKIE):
        import savedlisting
        with mock.patch.object(savedlisting, "fetch_page", side_effect=effect,
                               return_value=result) as fetch:
            ok, message = sw.test_login(username, cookie)
        self.assertNotIn(SESSION, message)
        self.assertNotIn("reddit_session", message)
        return ok, message, fetch

    def test_logged_in_does_not_read_as_a_total(self):
        """Page 1 only is fetched, so the message must not state a number of saves."""
        ok, message, fetch = self._run(result=([{}, {}, {}], None))
        self.assertEqual((ok, message), (True, "Logged in. Your saved list loads."))
        self.assertFalse(any(ch.isdigit() for ch in message))
        fetch.assert_called_once_with("https://old.reddit.com/user/some_user/saved/", cookie=COOKIE)

    def test_an_empty_saved_list_is_still_logged_in(self):
        self.assertEqual(self._run(result=([], None))[1], "Logged in. Your saved list loads.")

    def test_the_cookie_is_cleaned_before_it_is_used(self):
        _, _, fetch = self._run(result=([], None), cookie=f'"Cookie: {COOKIE}"')
        self.assertEqual(fetch.call_args.kwargs["cookie"], COOKIE)

    def test_expired_cookie(self):
        import savedlisting
        ok, message, _ = self._run(effect=savedlisting.SavedAuthError("403"))
        self.assertFalse(ok)
        self.assertIn("cookie has expired or is wrong", message)
        self.assertIn("Copy the cookie again", message)

    def test_login_for_a_different_account(self):
        import savedlisting
        ok, message, _ = self._run(
            effect=savedlisting.SavedAuthError("403", logged_in=True, account="Someone_Else"))
        self.assertFalse(ok)
        self.assertIn(f"different account than {USER}", message)
        self.assertIn("Someone_Else", message)

    def test_other_reasons(self):
        import redditclient
        import savedlisting
        cases = [
            (savedlisting.SavedRateLimited("429"), sw.MSG_TEST_BUSY),
            (redditclient.RedditFetchError("t", kind="ReadTimeout"), sw.MSG_TEST_TIMEOUT),
            (redditclient.RedditFetchError("x", kind="ConnectionError"), sw.MSG_TEST_OFFLINE),
            (RuntimeError(f"weird {COOKIE}"), "The test failed (RuntimeError)."),
        ]
        for exc, expected in cases:
            with self.subTest(exc=type(exc).__name__):
                ok, message, _ = self._run(effect=exc)
                self.assertEqual((ok, message), (False, expected))

    def test_bad_input_is_never_fetched(self):
        import savedlisting
        for username, cookie, hint in (("", COOKIE, sw.HINT_USER_EMPTY),
                                       (USER, "nope", sw.HINT_COOKIE_BAD),
                                       (USER, "", sw.HINT_COOKIE_EMPTY)):
            with self.subTest(username=username, cookie=cookie):
                with mock.patch.object(savedlisting, "fetch_page") as fetch:
                    self.assertEqual(sw.test_login(username, cookie), (False, hint))
                fetch.assert_not_called()

    def test_end_to_end_through_the_transport_with_a_403(self):
        """Real fetch_page + auth_message, only the HTTP layer mocked: a 403 with
        the logout marker is the wrong account, without it an expired cookie."""
        import redditclient
        for body, expect in ((forbidden(logged_in=True, account="Other"), "different account"),
                             (forbidden(logged_in=False), "expired")):
            with self.subTest(expect=expect):
                with mock.patch.object(redditclient, "get",
                                       return_value=FakeResponse(body, 403)) as get:
                    ok, message = sw.test_login(USER, COOKIE)
                self.assertFalse(ok)
                self.assertIn(expect, message)
                self.assertEqual(get.call_count, 1)             # exactly ONE fetch
                self.assertIn(SESSION, get.call_args.kwargs["headers"]["Cookie"])
                self.assertNotIn(SESSION, message)

    def test_end_to_end_good_page(self):
        import redditclient
        html = page([thing("t3_a", "https://i.redd.it/a.jpg", "i.redd.it"),
                     thing("t3_b", "https://i.redd.it/b.jpg", "i.redd.it")])
        with mock.patch.object(redditclient, "get", return_value=FakeResponse(html)):
            self.assertEqual(sw.test_login(USER, COOKIE), (True, "Logged in. Your saved list loads."))


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
        self.assertEqual(spec["arguments"], f'"{self.app / "src" / "app.py"}"')
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
        self.assertEqual(argv, ["py.exe", str(datahome.CODE_DIR / "setup_window.py"),
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


class FontChoiceTest(unittest.TestCase):
    def test_windows_keeps_segoe_ui(self):
        self.assertEqual(sw.choose_font_family({"Segoe UI", "Arial"}, "win32"), "Segoe UI")
        self.assertIsNone(sw.choose_font_family({"Arial"}, "win32"))

    def test_linux_takes_the_first_installed_in_order(self):
        self.assertEqual(sw.choose_font_family({"DejaVu Sans", "Cantarell"}, "linux"),
                         "Cantarell")
        self.assertEqual(sw.choose_font_family(set(sw.LINUX_FONTS), "linux"), "Noto Sans")
        self.assertEqual(sw.choose_font_family({"DejaVu Sans", "fixed"}, "linux"),
                         "DejaVu Sans")

    def test_nothing_known_keeps_tk_default(self):
        self.assertIsNone(sw.choose_font_family({"fixed", "helvetica"}, "linux"))
        self.assertIsNone(sw.choose_font_family({"Segoe UI"}, "darwin"))


class FakeGsettings:
    """subprocess.run stand-in: {key: stdout | exception}; records the calls."""

    def __init__(self, answers, returncode=0):
        self.answers, self.returncode, self.calls = answers, returncode, []

    def __call__(self, argv, **kwargs):
        self.calls.append((argv, kwargs))
        answer = self.answers.get(argv[-1], "")
        if isinstance(answer, BaseException):
            raise answer
        return mock.Mock(returncode=self.returncode, stdout=answer)


class LinuxThemeTest(unittest.TestCase):
    def test_color_scheme_prefer_dark(self):
        fake = FakeGsettings({"color-scheme": "'prefer-dark'\n"})
        self.assertTrue(sw._linux_is_dark(fake))
        argv, kwargs = fake.calls[0]
        self.assertEqual(argv, ["gsettings", "get", "org.gnome.desktop.interface",
                                "color-scheme"])
        self.assertEqual(kwargs["timeout"], 1)

    def test_gtk_theme_ending_in_dark(self):
        fake = FakeGsettings({"color-scheme": "'default'\n", "gtk-theme": "'Adwaita-dark'\n"})
        self.assertTrue(sw._linux_is_dark(fake))
        fake = FakeGsettings({"color-scheme": "'default'\n", "gtk-theme": "'Yaru'\n"})
        self.assertFalse(sw._linux_is_dark(fake))

    def test_missing_failing_or_slow_gsettings_is_light(self):
        import subprocess
        for answer in (FileNotFoundError(2, "gsettings"),
                       subprocess.TimeoutExpired("gsettings", 1), OSError(13, "denied")):
            with self.subTest(answer=type(answer).__name__):
                self.assertFalse(sw._linux_is_dark(
                    FakeGsettings({"color-scheme": answer, "gtk-theme": answer})))
        failing = FakeGsettings({"color-scheme": "'prefer-dark'"}, returncode=1)
        self.assertFalse(sw._linux_is_dark(failing))

    def test_resolve_theme_on_linux(self):
        with mock.patch.object(sw, "_linux_is_dark", return_value=True), \
             mock.patch.object(sw, "_system_is_dark", side_effect=AssertionError):
            self.assertEqual(sw.resolve_theme("system", platform="linux"), "dark")
            self.assertEqual(sw.resolve_theme("light", platform="linux"), "light")
        with mock.patch.object(sw, "_linux_is_dark", return_value=False):
            self.assertEqual(sw.resolve_theme("system", platform="linux"), "light")


class CookieDocsLinkTest(unittest.TestCase):
    def test_url_comes_from_the_repo_constant_and_the_anchor_exists(self):
        from doc_links import anchors
        self.assertEqual(sw.COOKIE_DOCS_URL,
                         f"{sw.GITHUB_REPO_URL}/blob/main/docs/SETUP.md#{sw.COOKIE_DOCS_ANCHOR}")
        self.assertIn(sw.COOKIE_DOCS_ANCHOR, anchors(REPO / "docs" / "SETUP.md"))


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
        win.user_var.set(f"u/{USER}")
        win.cookie_var.set(f"Cookie: {COOKIE}")
        win.archive_var.set(str(archive))
        with mock.patch.object(sw, "create_shortcut") as shortcut:
            self.assertTrue(win.save())
        shortcut.assert_not_called()
        self.assertEqual(win.result, sw.RESULT_SAVED)
        self.assertEqual(config.get(sw.COOKIE_KEY), COOKIE)   # prefix stripped
        self.assertEqual(config.get(sw.ARCHIVE_KEY), str(archive))
        self.assertEqual(config.get(sw.USER_KEY), USER)
        self.assertNotIn("REDDIT_SAVED_FEED_URL",
                         (self.root / "data" / "config.env").read_text(encoding="utf-8"))
        self.assertTrue(settings.setup_done())

    def _labels(self, win):
        out = []
        stack = [win.root]
        while stack:
            widget = stack.pop()
            stack.extend(widget.winfo_children())
            try:
                out.append(str(widget.cget("text")))
            except Exception:
                pass
        return out

    def test_feed_notice_shows_for_the_upgrader_only(self):
        os.environ[sw.ARCHIVE_KEY] = str(self.root)
        win = self._window()
        self.assertNotIn(sw.FEED_NOTICE, self._labels(win))   # fresh install
        win.root.destroy()
        self.tk_root = _tk_root()
        os.environ[sw.FEED_KEY] = NeedsSetupTest.FEED
        win = self._window()
        self.assertIn(sw.FEED_NOTICE, self._labels(win))      # upgrader

    def test_save_with_shortcut_checked_creates_it(self):
        win = self._window()
        win.user_var.set(USER)
        win.cookie_var.set(COOKIE)
        win.shortcut_var.set(True)
        with mock.patch.object(sw, "create_shortcut", return_value=(True, "ok")) as shortcut, \
             contextlib.redirect_stdout(io.StringIO()):
            win.save()
        self.assertEqual(shortcut.call_count, 1 if sw.shortcut_supported() else 0)

    def test_save_refuses_a_bad_cookie_and_writes_nothing(self):
        win = self._window()
        win.user_var.set(USER)
        win.cookie_var.set("loid=SECRETOTHERVALUE; over18=1")
        self.assertFalse(win.save())
        self.assertEqual(win.cookie_msg.cget("text"), sw.HINT_COOKIE_BAD)
        self.assertEqual(self.files(), [])
        self.assertNotEqual(win.result, sw.RESULT_SAVED)

    def test_username_and_cookie_are_required(self):
        win = self._window()
        self.assertFalse(win.save())
        self.assertEqual(win.user_msg.cget("text"), sw.HINT_USER_EMPTY)
        self.assertEqual(win.cookie_msg.cget("text"), sw.HINT_COOKIE_EMPTY)
        self.assertEqual(self.files(), [])
        win.user_var.set("ab")
        self.assertFalse(win.save())
        self.assertEqual(win.user_msg.cget("text"), sw.HINT_USER_BAD)

    def test_cookie_round_trips_through_config_env(self):
        win = self._window()
        win.user_var.set(USER)
        win.cookie_var.set(COOKIE)
        self.assertTrue(win.save())
        self.assertEqual(config.get(sw.COOKIE_KEY), COOKIE)
        self.assertIn(SESSION, config.secret_values())

    def test_test_button_runs_the_login_test_with_what_is_typed(self):
        win = self._window()
        win.user_var.set(USER)
        win.cookie_var.set(COOKIE)
        with mock.patch.object(sw, "test_login", return_value=(True, sw.MSG_TEST_OK)) as run:
            win.start_test()
            kind, payload = win._events.get(timeout=2)
        run.assert_called_once_with(USER, COOKIE)
        self.assertEqual((kind, payload), ("test", (True, sw.MSG_TEST_OK)))
        self.assertIn("disabled", win.test_btn.state())

    def test_test_button_validates_before_fetching(self):
        win = self._window()
        with mock.patch.object(sw, "test_login") as run:
            win.start_test()
            self.assertEqual(win.user_msg.cget("text"), sw.HINT_USER_EMPTY)
            win.user_var.set(USER)
            win.start_test()
            self.assertEqual(win.cookie_msg.cget("text"), sw.HINT_COOKIE_EMPTY)
        run.assert_not_called()
        self.assertNotIn("disabled", win.test_btn.state())

    def test_missing_archive_is_created_only_after_confirm(self):
        target = self.root.parent / f"{self.root.name}-new" / "Archive"
        self.addCleanup(shutil.rmtree, target.parent, True)
        win = self._window(confirm=lambda *a: False)
        win.user_var.set(USER)
        win.cookie_var.set(COOKIE)
        win.archive_var.set(str(target))
        self.assertFalse(win.save())
        self.assertFalse(target.exists())
        self.assertEqual(self.files(), [])
        win = self._window(confirm=lambda *a: True)
        win.user_var.set(USER)
        win.cookie_var.set(COOKIE)
        win.archive_var.set(str(target))
        self.assertTrue(win.save())
        self.assertTrue(target.is_dir())

    def test_skip_writes_only_setup_done(self):
        win = self._window()
        win.cookie_var.set(COOKIE)  # typed but skipped -> not saved
        win.skip()
        self.assertEqual(win.result, sw.RESULT_SKIPPED)
        self.assertEqual(self.files(), [os.path.join("data", "settings.json")])
        self.assertEqual(self.settings_json(), {"setup_done": True})

    def test_cancel_writes_nothing(self):
        win = self._window()
        win.cookie_var.set(COOKIE)
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
        win.user_var.set(USER)
        win.cookie_var.set(COOKIE)
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

    def test_cookie_is_masked_until_show(self):
        win = self._window()
        self.assertEqual(win.cookie_entry.cget("show"), "\u2022")
        win.show_var.set(True)
        win._toggle_show()
        self.assertEqual(win.cookie_entry.cget("show"), "")

    def test_empty_messages_take_no_space(self):
        win = self._window()
        for label in (win.cookie_msg, win.user_msg, win.archive_msg):
            self.assertEqual(label.winfo_manager(), "")  # not gridded while empty
        win._say(win.cookie_msg, "Testing...")
        self.assertEqual(win.cookie_msg.winfo_manager(), "grid")
        win.cookie_var.set("x")  # editing the cookie clears the message again
        self.assertEqual(win.cookie_msg.winfo_manager(), "")

    def test_a_cookie_from_the_environment_is_read_only_and_testable(self):
        os.environ[sw.COOKIE_KEY] = COOKIE
        os.environ[sw.USER_KEY] = USER
        win = self._window()
        self.assertIsNone(win.cookie_entry)
        with mock.patch.object(sw, "test_login", return_value=(True, "ok")) as run:
            win.start_test()
            win._events.get(timeout=2)
        run.assert_called_once_with(USER, COOKIE)
        self.assertEqual(sw.editable_keys().count(sw.COOKIE_KEY), 0)

    def test_cookie_link_opens_the_setup_section(self):
        win = self._window()
        self.assertEqual(win.cookie_link.cget("text"), sw.COOKIE_LINK)
        with mock.patch.object(sw.webbrowser, "open") as opened:
            win.cookie_link.invoke()
        opened.assert_called_once_with(sw.COOKIE_DOCS_URL)

    def test_checkbox_draws_a_check_mark_image(self):
        win = self._window()
        layout = repr(win.ttk.Style(self.tk_root).layout("TCheckbutton"))
        self.assertIn("oshiire_check", layout)
        off, on = win._check_images
        self.assertEqual((off.width(), off.height()), (on.width(), on.height()))
        # The middle of the "on" box is the accent colour or the mark, never the
        # empty box colour.
        size = on.height()
        accent = self.tk_root.winfo_rgb(win.colors["btn"])
        self.assertEqual(tuple(c * 257 for c in on.get(size // 2, size // 5)), accent)

    def test_headings_use_the_body_family(self):
        from tkinter import font as tkfont
        win = self._window()
        body = tkfont.nametofont("TkDefaultFont", root=self.tk_root).actual("family")
        expected = sw.choose_font_family(tkfont.families(self.tk_root)) or body
        for font, size in ((win.title_font, sw.TITLE_SIZE), (win.head_font, sw.HEAD_SIZE)):
            self.assertEqual(font.actual("family"), expected)
            self.assertEqual(font.actual("weight"), "bold")
            self.assertEqual(font.cget("size"), size)


if __name__ == "__main__":
    unittest.main(verbosity=2)
