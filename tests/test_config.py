"""Tests for config.py -- merged settings and the app-written data/config.env.

Every test runs inside config_sandbox.isolated_config: a temp data root as the
cwd, an empty temp "app folder", and the four config keys removed from the
process environment, so nothing here can see the real .env.

    python -m unittest discover -s tests
"""
import os
import subprocess
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))  # config_sandbox

import config
from config_sandbox import isolated_config
from dotenv import dotenv_values

FEED = "REDDIT_SAVED_FEED_URL"
ARCH = "ARCHIVE_DIR"
USER = "REDDIT_USERNAME"
COOKIE = "REDDIT_SESSION_COOKIE"


class _Sandbox(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix="cfg-root-")).resolve()
        self.app = Path(tempfile.mkdtemp(prefix="cfg-app-")).resolve()
        self._ctx = isolated_config(self.root, app_dir=self.app)
        self._ctx.__enter__()

    def tearDown(self):
        self._ctx.__exit__(None, None, None)

    def write(self, path, text):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(textwrap.dedent(text).lstrip(), encoding="utf-8")

    @property
    def root_env(self):
        return self.root / ".env"

    @property
    def app_env(self):
        return self.app / ".env"

    @property
    def config_env(self):
        return self.root / "data" / "config.env"


class PrecedenceTest(_Sandbox):
    def test_each_layer_beats_the_ones_below(self):
        os.environ[FEED] = "from-env"
        self.write(self.root_env, f"{FEED}=from-root-env\n{ARCH}=from-root-env\n")
        self.write(self.app_env, f"{FEED}=from-app-env\n{ARCH}=from-app-env\n{USER}=from-app-env\n")
        self.write(self.config_env, f"{FEED}=c\n{ARCH}=c\n{USER}=c\n{COOKIE}=from-config\n")
        self.assertEqual(config.get(FEED), "from-env")
        self.assertEqual(config.get(ARCH), "from-root-env")
        self.assertEqual(config.get(USER), "from-app-env")
        self.assertEqual(config.get(COOKIE), "from-config")
        self.assertEqual(config.key_sources(),
                         {FEED: "env", ARCH: ".env", USER: ".env", COOKIE: "config"})

    def test_unset_everywhere(self):
        self.assertEqual(config.get(FEED), "")
        self.assertEqual(config.key_sources(), {k: None for k in config.CONFIG_KEYS})

    def test_same_file_is_read_once(self):
        # A git checkout: the data root IS the app folder.
        files = config.source_files(self.root, self.root)
        self.assertEqual([s for s, _ in files], [".env", "config"])
        files = config.source_files(self.root, self.app)
        self.assertEqual([s for s, _ in files], [".env", ".env", "config"])

    def test_empty_file_value_does_not_claim_a_setting(self):
        # .env copied from .env.example has REDDIT_SAVED_FEED_URL= (empty): the
        # value entered in the app must still apply.
        self.write(self.root_env, f"{FEED}=\n")
        self.write(self.config_env, f"{FEED}='https://www.reddit.com/saved.rss?feed=abc'\n")
        self.assertEqual(config.get(FEED), "https://www.reddit.com/saved.rss?feed=abc")
        self.assertEqual(config.key_sources()[FEED], "config")


class LoadTest(_Sandbox):
    def test_load_never_overrides_and_fills_the_rest(self):
        os.environ[FEED] = "process-value"
        self.write(self.root_env, f"{FEED}=file-value\nOTHER_DOTENV_KEY=yes\n")
        self.write(self.config_env, f"{ARCH}=C:/Archive\n")
        config.load()
        self.assertEqual(os.environ[FEED], "process-value")  # untouched
        self.assertEqual(os.environ[ARCH], "C:/Archive")     # filled from config.env
        self.assertEqual(os.environ["OTHER_DOTENV_KEY"], "yes")  # .env loads every key
        self.assertEqual(config.key_sources()[FEED], "env")
        self.assertEqual(config.key_sources()[ARCH], "config")

    def test_config_env_contributes_only_the_four_keys(self):
        self.write(self.config_env, f"{ARCH}=C:/A\nSOMETHING_ELSE=nope\nPATH=hijack\n")
        prev_path = os.environ.get("PATH")
        os.environ.pop("SOMETHING_ELSE", None)
        config.load()
        self.assertNotIn("SOMETHING_ELSE", os.environ)
        self.assertEqual(os.environ.get("PATH"), prev_path)
        self.assertEqual(os.environ[ARCH], "C:/A")

    def test_load_sets_the_dotenv_kill_switch(self):
        config.load()
        self.assertEqual(os.environ[config.DISABLE_FLAG], "1")

    def test_launch_environ_is_the_pre_load_snapshot(self):
        self.write(self.root_env, f"{ARCH}=C:/A\n")
        config.load()
        self.assertEqual(os.environ[ARCH], "C:/A")
        env = config.launch_environ()
        self.assertNotIn(ARCH, env)
        self.assertNotIn(config.DISABLE_FLAG, env)

    def test_summary_names_keys_never_values(self):
        secret = "SUPERSECRETcookieVALUE99"
        self.write(self.root_env, f"{COOKIE}=reddit_session={secret}\n")
        self.write(self.config_env, f"{ARCH}='D:/My Archive'\n")
        config.load()
        line = config.summary()
        self.assertNotIn(secret, line)
        self.assertNotIn("My Archive", line)
        self.assertIn(f"{COOKIE} [.env]", line)
        self.assertIn(f"{ARCH} [config]", line)
        self.assertIn(f"{FEED}", line)  # listed as unset
        self.assertEqual(line.count("\n"), 0)

    def test_secret_values_come_from_the_merged_config(self):
        self.write(self.root_env, f"{FEED}=https://www.reddit.com/saved.rss?feed=FEEDtokenFROMdotenv&user=me\n")
        self.write(self.config_env,
                   f"{COOKIE}='reddit_session=COOKIEfromCONFIGlonger123; loid=abcdefgh12345; ab=sh'\n")
        secrets = config.secret_values()
        self.assertEqual(secrets, ["COOKIEfromCONFIGlonger123",  # longest first
                                   "FEEDtokenFROMdotenv", "abcdefgh12345"])


class DotenvFlagTest(unittest.TestCase):
    """PYTHON_DOTENV_DISABLED (set by config.load) turns a no-argument
    load_dotenv() into a no-op, so a stray .env in a PARENT of the app folder is
    never picked up; an explicit-path dotenv_values() is unaffected. Run in a
    subprocess so os.environ of this test process is never touched."""

    def setUp(self):
        self.parent = Path(tempfile.mkdtemp(prefix="cfg-stray-"))
        (self.parent / ".env").write_text("STRAY_KEY=from-a-parent-folder\n", encoding="utf-8")
        self.child = self.parent / "Oshiire"
        self.child.mkdir()
        (self.child / "mod.py").write_text(textwrap.dedent("""
            import os
            from dotenv import load_dotenv, dotenv_values
            load_dotenv()
            print("loaded=" + str(os.environ.get("STRAY_KEY")))
            print("explicit=" + str(dotenv_values(os.path.join(os.path.dirname(os.path.dirname(
                os.path.abspath(__file__))), ".env")).get("STRAY_KEY")))
        """), encoding="utf-8")

    def _run(self, disabled):
        env = {k: v for k, v in os.environ.items() if k not in ("STRAY_KEY", config.DISABLE_FLAG)}
        if disabled:
            env[config.DISABLE_FLAG] = "1"
        out = subprocess.run([sys.executable, str(self.child / "mod.py")], cwd=str(self.child),
                             env=env, capture_output=True, text=True, timeout=60)
        self.assertEqual(out.returncode, 0, out.stderr)
        return out.stdout

    def test_without_the_flag_the_stray_env_is_loaded(self):
        # The control: proves the fixture really is reachable by the upward walk.
        self.assertIn("loaded=from-a-parent-folder", self._run(disabled=False))

    def test_with_the_flag_the_stray_env_is_not_loaded(self):
        out = self._run(disabled=True)
        self.assertIn("loaded=None", out)
        # ...while an explicit-path read (what config.py does) still works.
        self.assertIn("explicit=from-a-parent-folder", out)


class SaveSetupTest(_Sandbox):
    CASES = {
        ARCH: r"C:\Users\Some One\Google Drive\Art Archive" + "\\",
        FEED: "https://www.reddit.com/saved.rss?feed=ab12CD&user=some_one&x=a=b",
        COOKIE: 'reddit_session=a"b\\c\'d; token_v2=\\\\server\\share "quoted" \\\'',
        USER: "",
    }

    def test_round_trip_is_byte_identical(self):
        config.save_setup(self.CASES)
        raw = dotenv_values(self.config_env, interpolate=False)
        self.assertEqual(raw, self.CASES)
        # And through config's own reader (USER is empty, so it claims nothing).
        for key in (ARCH, FEED, COOKIE):
            self.assertEqual(config.get(key), self.CASES[key])
        self.assertEqual(config.get(USER, None), None)

    def test_round_trip_values_that_need_quoting(self):
        # Each forces the single-quoted form: a leading quote, leading/trailing
        # whitespace, whitespace + "#", and quotes/backslashes inside.
        cases = [
            "'starts with a quote",
            '"double first',
            "  padded  ",
            r"D:\Art #2\inbox",
            "ends with an escaped quote \\'",
            "mid \\' and \" and \\\\ ",
        ]
        for value in cases:
            with self.subTest(value=value):
                config.save_setup({ARCH: value, FEED: r"C:\after\it" + "\\"})
                raw = dotenv_values(self.config_env, interpolate=False)
                self.assertEqual(raw[ARCH], value)
                self.assertEqual(raw[FEED], r"C:\after\it" + "\\")

    def test_unstorable_value_is_refused_and_nothing_written(self):
        # Needs quoting (whitespace + "#") AND ends in a backslash, with a later
        # line that holds a single quote: the parser reads the closing quote as
        # an escaped one and runs on into that line. Refused, never stored wrong.
        config.save_setup({ARCH: "C:/kept"})
        before = self.config_env.read_bytes()
        with self.assertRaises(ValueError):
            config.save_setup({ARCH: "D:\\Art #2\\", USER: "o'neil"})
        self.assertEqual(self.config_env.read_bytes(), before)

    def test_never_writes_dotenv(self):
        config.save_setup({ARCH: "C:/A"})
        self.assertFalse(self.root_env.exists())
        self.assertFalse(self.app_env.exists())
        self.assertTrue(self.config_env.is_file())

    def test_keeps_other_keys_and_writes_only_the_four(self):
        config.save_setup({FEED: "https://x/?feed=1", ARCH: "C:/A"})
        config.save_setup({ARCH: "D:/B"})
        raw = dotenv_values(self.config_env, interpolate=False)
        self.assertEqual(raw, {FEED: "https://x/?feed=1", ARCH: "D:/B"})

    def test_rejects_unknown_keys_and_multiline_values(self):
        with self.assertRaises(ValueError):
            config.save_setup({"PATH": "x"})
        with self.assertRaises(ValueError):
            config.save_setup({ARCH: "line1\nline2"})
        with self.assertRaises(ValueError):
            config.save_setup({ARCH: 5})
        self.assertFalse(self.config_env.exists())  # nothing written on refusal

    def test_atomic_no_temp_left_behind(self):
        config.save_setup({ARCH: "C:/A"})
        leftovers = [p.name for p in self.config_env.parent.iterdir() if p.name != "config.env"]
        self.assertEqual(leftovers, [])

    def test_refreshes_os_environ_after_load_but_not_process_keys(self):
        os.environ[FEED] = "process-value"
        config.load()
        config.save_setup({FEED: "https://new/?feed=2", ARCH: "C:/New"})
        self.assertEqual(os.environ[FEED], "process-value")
        self.assertEqual(os.environ[ARCH], "C:/New")
        config.save_setup({ARCH: ""})  # cleared -> no longer set
        self.assertNotIn(ARCH, os.environ)

    @unittest.skipIf(sys.platform == "win32", "POSIX permissions")
    def test_file_mode_is_0600_on_posix(self):
        config.save_setup({ARCH: "/a"})
        self.assertEqual(self.config_env.stat().st_mode & 0o777, 0o600)


if __name__ == "__main__":
    unittest.main(verbosity=2)
