"""Tests for the release pieces: version.py, requirements-lock.txt,
tools/build_release.py's file selection and checks, and the release workflow's
fixed names and pins. Nothing here builds a release or touches the network.

    python -m unittest discover -s tests
"""
import importlib.util
import re
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

import useragent
import version


def _load_build_release():
    spec = importlib.util.spec_from_file_location("build_release", REPO / "tools" / "build_release.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


br = _load_build_release()
norm = lambda n: re.sub(r"[-_.]+", "-", n).lower()


class VersionTest(unittest.TestCase):
    def test_user_agent_carries_the_app_version(self):
        self.assertEqual(useragent.VERSION, f"v{version.__version__}")
        self.assertIn(f"oshiire:v{version.__version__} ", useragent.build_user_agent())

    def test_changelog_has_a_section_for_this_version(self):
        text = (REPO / "CHANGELOG.md").read_text(encoding="utf-8")
        self.assertRegex(text, rf"(?m)^## \[{re.escape(version.__version__)}\]")

    def test_build_reads_the_same_version(self):
        self.assertEqual(br.app_version(), version.__version__)


class LockFileTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.lines = [l.strip() for l in (REPO / "requirements-lock.txt").read_text(
            encoding="utf-8").splitlines() if l.strip() and not l.startswith("#")]
        cls.pins = {}
        cls.markers = {}
        for line in cls.lines:
            spec, _, marker = line.partition(";")
            name, version_ = spec.strip().split("==")
            cls.pins[norm(name)] = version_
            if marker:
                cls.markers[norm(name)] = marker.strip()

    def test_every_line_is_an_exact_pin(self):
        for line in self.lines:
            self.assertRegex(line, r'^[A-Za-z0-9._-]+==[^\s;@]+(; [a-z_]+ == "[A-Za-z0-9]+")?$', line)

    def test_dev_chain_and_pip_are_out(self):
        for name in ("playwright", "pyee", "greenlet", "fonttools", "pip"):
            self.assertNotIn(name, self.pins)

    def test_markers(self):
        self.assertEqual(self.markers, {
            "pythonnet": 'sys_platform == "win32"',
            "clr-loader": 'sys_platform == "win32"',
            "colorama": 'platform_system == "Windows"',
        })

    def test_numpy_two_despite_the_imgutils_pin(self):
        self.assertTrue(self.pins["numpy"].startswith("2."))
        self.assertIn("--no-deps", (REPO / "requirements-lock.txt").read_text(encoding="utf-8"))

    def test_requirements_txt_is_covered(self):
        for line in (REPO / "requirements.txt").read_text(encoding="utf-8").splitlines():
            line = line.split("#", 1)[0].strip()
            if line:
                name = re.split(r"[=<>~! \[]", line, maxsplit=1)[0]
                self.assertIn(norm(name), self.pins, name)

    def test_source_only_packages_are_pinned(self):
        self.assertEqual(br.SOURCE_ONLY, ("bchlib", "proxy_tools", "sgmllib3k"))
        self.assertEqual(br.source_only_reqs(),
                         ["bchlib==2.1.3", "proxy_tools==0.1.0", "sgmllib3k==1.0.0"])


class AppFileSelectionTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if shutil.which("git") is None or not (REPO / ".git").exists():
            raise unittest.SkipTest("needs a git checkout")
        cls.files = br.repo_files()
        cls.win, cls.modules, cls.left_out = br.select_app_files("windows", cls.files)
        cls.linux, _, _ = br.select_app_files("linux", cls.files)

    def test_modules_come_from_the_import_walk(self):
        for module in ("app", "setup_window", "review", "jobs", "config", "datahome",
                       "starters", "version", "ingest", "backfill", "archive", "ai_tag",
                       "ai_tagger", "hash_index", "imagemeta", "export_unsave_list",
                       "tombstones", "redditclient"):
            self.assertIn(module, self.modules)
            self.assertIn(f"{module}.py", self.win)
        self.assertEqual(self.left_out, ["calibrate.py"])

    def test_static_files_and_launchers(self):
        for rel in ("assets/fonts/OFL.txt", "data/tombstones.json", "layout.example.json",
                    "data/backfill/state.example.json", ".env.example",
                    "oshiire_unsave.user.js", "README.md", "LICENSE", "CHANGELOG.md",
                    "docs/SETUP.md", "docs/BACKFILL.md"):
            self.assertIn(rel, self.win, rel)
        self.assertIn("Oshiire.bat", self.win)
        self.assertNotIn("oshiire.sh", self.win)
        self.assertIn("oshiire.sh", self.linux)
        self.assertNotIn("Oshiire.bat", self.linux)

    def test_nothing_dev_only_or_personal(self):
        for rel in self.win + self.linux:
            self.assertFalse(rel.startswith(("tests/", "scripts/", "screenshots/", "tools/",
                                             ".github/", "staging/")), rel)
            self.assertNotIn(rel, ("CLAUDE.md", "blueprint.md", "install.bat", "install.sh",
                                   "calibrate.py", "manifest.json", "layout.json",
                                   "subreddit_map.json", ".env", "docs/PROJECT_CONTEXT.md"))
            self.assertFalse(rel.startswith("requirements"), rel)
        self.assertEqual([r for r in self.win if r.startswith("data/")
                          and ".example." not in r], ["data/tombstones.json"])


class TreeScanTest(unittest.TestCase):
    def setUp(self):
        self.top = Path(tempfile.mkdtemp(prefix="br-scan-")) / "Oshiire"
        self.top.mkdir()

    def tearDown(self):
        shutil.rmtree(self.top.parent, ignore_errors=True)

    def touch(self, rel):
        path = self.top / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("x", encoding="utf-8")

    def test_clean_tree_passes(self):
        for rel in ("app.py", "data/tombstones.json", "data/backfill/state.example.json",
                    "manifest.example.json", "runtime/Lib/site-packages/gradio/templates/manifest.json",
                    "runtime/Lib/site-packages/pkg/layout.json"):
            self.touch(rel)
        self.assertEqual(br.scan_tree(self.top, "windows"), [])

    def test_personal_files_fail(self):
        bad = ["manifest.json", "layout.json", "subreddit_map.json", "known_series_names.txt",
               "000___Known_Series_Names.txt", ".env", "staging/t3_x.jpg",
               "data/settings.json", "data/config.env", "data/archive_index.db",
               "data/archive_index.db-wal", "data/oshiire.log", "data/oshiire.lock",
               "data/thumbs/a.jpg", "data/backfill/state.json", "data/saved_posts.csv",
               "data/unsave_list.json", "runtime/Lib/.env", "docs/config.env"]
        for rel in bad:
            self.touch(rel)
        found = "\n".join(br.scan_tree(self.top, "linux"))
        for rel in bad:
            self.assertIn(f"personal file in the release: {rel}", found)

    def test_long_paths_fail_on_windows_only(self):
        rel = "runtime/" + "d" * 60 + "/" + "f" * (br.MAX_WINDOWS_PATH - 60) + ".py"
        self.touch(rel)
        self.assertTrue(any("longer than" in p for p in br.scan_tree(self.top, "windows")))
        self.assertEqual(br.scan_tree(self.top, "linux"), [])

    def test_windows_pth_isolates_the_runtime(self):
        runtime = self.top / "runtime"
        runtime.mkdir()
        pth = br.isolate_runtime(runtime, "windows", Path(sys.executable))
        expected = f"python{sys.version_info[0]}{sys.version_info[1]}._pth"
        self.assertEqual(pth.name, expected)
        lines = pth.read_text(encoding="ascii").splitlines()
        self.assertIn("Lib\\site-packages", lines)
        self.assertIn("..", lines)  # the app folder
        self.assertNotIn("import site", lines)  # no per-user site-packages
        self.assertIsNone(br.isolate_runtime(runtime, "linux", Path(sys.executable)))


class WorkflowTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.text = (REPO / ".github" / "workflows" / "release.yml").read_text(encoding="utf-8")
        import yaml
        cls.doc = yaml.safe_load(cls.text)

    def test_archive_names_are_fixed(self):
        self.assertEqual(br.ARCHIVE_NAMES, {"windows": "Oshiire-Windows.zip",
                                            "linux": "Oshiire-Linux.tar.gz"})
        for name in br.ARCHIVE_NAMES.values():
            self.assertIn(f"dist/{name}", self.text)
            self.assertIn(f"out/{name}", self.text)

    def test_runtime_pins(self):
        env = self.doc["env"]
        self.assertEqual(env["PBS_RELEASE"], "20260804")
        self.assertEqual(env["WIN_SHA256"],
                         "06e357ff7e5d1629b53908e412453e527708fa91e6e78f1a98dc0c30b6fef878")
        self.assertEqual(env["LINUX_SHA256"],
                         "8fa4614f8c2d47430d5573098fad787c44bb314830fd2e826952335446706e45")
        self.assertTrue(env["WIN_ASSET"].startswith("cpython-3.14.6+20260804-x86_64-pc-windows"))
        self.assertTrue(env["LINUX_ASSET"].startswith("cpython-3.14.6+20260804-x86_64-unknown-linux-gnu"))

    def test_permissions(self):
        self.assertEqual(self.doc["permissions"], {"contents": "read"})
        jobs = self.doc["jobs"]
        self.assertEqual(jobs["release"]["permissions"], {"contents": "write"})
        for name in ("windows", "linux"):
            self.assertNotIn("permissions", jobs[name])

    def test_release_only_on_tag_push(self):
        release = self.doc["jobs"]["release"]
        self.assertIn("github.event_name == 'push'", release["if"])
        self.assertEqual(sorted(release["needs"]), ["linux", "windows"])

    def test_only_official_actions(self):
        uses = re.findall(r"uses:\s*(\S+)", self.text)
        self.assertTrue(uses)
        for ref in uses:
            self.assertRegex(ref, r"^actions/[a-z-]+@v\d+$")


if __name__ == "__main__":
    unittest.main(verbosity=2)
